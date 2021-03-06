import datetime
import os
import re
import typing
import warnings
from typing import Dict, List, Optional

import dateutil.parser

from .base import BaseParser
from ..definition import Definition
from ..metadata import Metadata, Subset
from ..term import Term
from ..synonym import SynonymData, SynonymType
from ..relationship import Relationship
from ..pv import ResourcePropertyValue, LiteralPropertyValue
from ..xref import Xref
from ..utils.impl import etree
from ..utils.warnings import SyntaxWarning, NotImplementedWarning

if typing.TYPE_CHECKING:
    from ..entity import Entity
    from ..ontology import Ontology


class Namespace(object):
    def __init__(self, base: str):
        self.base = base

    def __getitem__(self, item: str) -> str:
        return f"{{{self.base}}}{item}"

    def raw(self, item: str) -> str:
        return f"{self.base}{item}"


_NS = {
    "dc": Namespace("http://purl.org/dc/elements/1.1/"),
    "doap": Namespace("http://usefulinc.com/ns/doap#"),
    "foaf": Namespace("http://xmlns.com/foaf/0.1/"),
    "meta": Namespace("http://www.co-ode.org/ontologies/meta.owl#"),
    "obo": Namespace("http://purl.obolibrary.org/obo/"),
    "oboInOwl": Namespace("http://www.geneontology.org/formats/oboInOwl#"),
    "owl": Namespace("http://www.w3.org/2002/07/owl#"),
    "protege": Namespace("http://protege.stanford.edu/plugins/owl/protege#"),
    "rdf": Namespace("http://www.w3.org/1999/02/22-rdf-syntax-ns#"),
    "rdfs": Namespace("http://www.w3.org/2000/01/rdf-schema#"),
    "skos": Namespace("http://www.w3.org/2004/02/skos/core#"),
    "ubprop": Namespace("http://purl.obolibrary.org/obo/ubprop#"),
    "uberon": Namespace("http://purl.obolibrary.org/obo/uberon#"),
    "xsd": Namespace("http://www.w3.org/2001/XMLSchema#"),
}

_SYNONYMS = {
    _NS["oboInOwl"].raw("hasExactSynonym"): "EXACT",
    _NS["oboInOwl"].raw("hasBroadSynonym"): "BROAD",
    _NS["oboInOwl"].raw("hasNarrowSynonym"): "NARROW",
    _NS["oboInOwl"].raw("hasRelatedSynonym"): "RELATED",
}

_SYNONYMS_ATTRIBUTES = {
    _NS["oboInOwl"]["hasExactSynonym"]: "EXACT",
    _NS["oboInOwl"]["hasBroadSynonym"]: "BROAD",
    _NS["oboInOwl"]["hasNarrowSynonym"]: "NARROW",
    _NS["oboInOwl"]["hasRelatedSynonym"]: "RELATED",
}


class RdfXMLParser(BaseParser):
    """A parser for OWL2 ontologies serialized in RDF/XML format.

    To Do:
        * Extraction of annotation properties, which are currently simply
          being ignored when parsing a document.
    """

    # -- BaseParser interface ------------------------------------------------

    @classmethod
    def can_parse(cls, path, buffer):
        return buffer.lstrip().startswith((b"<?xml", b"<rdf:RDF", b"<owl:"))

    def parse_from(self, handle):
        # Load the XML document into an XML Element tree
        tree: etree.ElementTree = etree.parse(handle)

        # Keep a map of aliases (IRI -> local OBO id)
        aliases: Dict[str, str] = dict()

        # Load metadata from the `owl:Ontology` element
        owl_ontology = tree.find(_NS["owl"]["Ontology"])
        if owl_ontology is None:
            raise ValueError("could not find `owl:Ontology` element")
        self.ont.metadata = self._extract_meta(owl_ontology)

        # Process imports
        self.ont.imports.update(
            self.process_imports(
                self.ont.metadata.imports,
                self.ont.import_depth,
                os.path.dirname(self.ont.path or str()),
                self.ont.timeout,
            )
        )

        # Parse typedef first to handle OBO shorthand renaming
        for prop in tree.iterfind(_NS["owl"]["ObjectProperty"]):
            self._extract_object_property(prop, aliases)
        for prop in tree.iterfind(_NS["owl"]["AnnotationProperty"]):
            self._extract_annotation_property(prop, aliases)
        for class_ in tree.iterfind(_NS["owl"]["Class"]):
            self._extract_term(class_, aliases)
        for axiom in tree.iterfind(_NS["owl"]["Axiom"]):
            self._process_axiom(axiom, aliases)

    # -- Helper methods ------------------------------------------------------

    def _compact_id(self, iri: str) -> str:
        """Compact an OBO identifier into a prefixed identifier.
        """
        match = re.match("^http://purl.obolibrary.org/obo/([^#_]+)_(.*)$", iri)
        if match is not None:
            return ":".join(match.groups())
        if self.ont.metadata.ontology is not None:
            id_ = self.ont.metadata.ontology
            match = re.match(f"^http://purl.obolibrary.org/obo/{id_}#(.*)$", iri)
            if match is not None:
                return match.group(1)
        return iri

    def _compact_datatype(self, iri: str) -> str:
        match = re.match("^http://www.w3.org/2001/XMLSchema#(.*)$", iri)
        if match is not None:
            return f"xsd:{match.group(1)}"
        raise ValueError(f"invalid datatype: {iri!r}")

    def _extract_resource_pv(self, elem: etree.Element) -> ResourcePropertyValue:
        property = re.sub("{|}", "", elem.tag)
        resource = elem.attrib[_NS["rdf"]["resource"]]
        return ResourcePropertyValue(property, resource)

    def _extract_literal_pv(self, elem: etree.Element) -> LiteralPropertyValue:
        property = re.sub("{|}", "", elem.tag)
        datatype = elem.get(_NS["rdf"]["datatype"])
        if datatype is None:
            warnings.warn(
                f"{elem} contains text but no `xsd:datatype`",
                SyntaxWarning,
                stacklevel=2,
            )
            datatype = _NS["xsd"].raw("string")
        return LiteralPropertyValue(
            property, typing.cast(str, elem.text), self._compact_datatype(datatype)
        )

    def _extract_meta(self, elem: etree.Element):
        """Extract the metadata from an `owl:Ontology` element.
        """
        meta = Metadata()
        if __debug__:
            if elem.tag != _NS["owl"]["Ontology"]:
                raise ValueError("expected `owl:Ontology` element")

        # extract OBO format version
        iri = elem.get(_NS["rdf"]["about"])
        if iri is not None:
            match = re.match("^http://purl.obolibrary.org/obo/(.*).(obo|owl)$", iri)
            meta.ontology = match.group(1) if match is not None else iri

        # extract metadata from child elements
        for child in elem:
            if child.tag == _NS["rdfs"]["comment"] and child.text is not None:
                meta.remarks.add(child.text)
            elif child.tag == _NS["oboInOwl"]["hasOBOFormatVersion"]:
                meta.format_version = child.text
            elif child.tag in (_NS["oboInOwl"]["saved-by"], _NS["oboInOwl"]["savedBy"]):
                meta.saved_by = child.text
            elif child.tag == _NS["oboInOwl"]["auto-generated-by"]:
                meta.auto_generated_by = child.text
            elif child.tag in (
                _NS["oboInOwl"]["default-namespace"],
                _NS["oboInOwl"]["hasDefaultNamespace"],
            ):
                meta.default_namespace = child.text
            elif child.tag == _NS["oboInOwl"]["date"]:
                meta.date = datetime.datetime.strptime(child.text, "%d:%m:%Y %H:%M")
            elif child.tag == _NS["oboInOwl"]["NamespaceIdRule"]:
                meta.namespace_id_rule = child.text
            elif child.tag == _NS["owl"]["imports"]:
                meta.imports.add(child.get(_NS["rdf"]["resource"]))
            elif child.tag == _NS["owl"]["versionIRI"]:
                iri = child.get(_NS["rdf"]["resource"])
                if iri is not None and meta.ontology is not None:
                    rx = "^http://purl.obolibrary.org/obo/{0}/(.*)/{0}.(obo|owl)$"
                    match = re.match(rx.format(meta.ontology), iri)
                else:
                    match = None
                meta.data_version = iri if match is None else match.group(1)
            elif child.tag == _NS["doap"]["Version"]:
                meta.data_version = child.text
            elif _NS["rdf"]["resource"] in child.attrib:
                meta.annotations.add(self._extract_resource_pv(child))
            elif child.text is not None:
                meta.annotations.add(self._extract_literal_pv(child))
            else:
                warnings.warn(
                    f"unknown element in `owl:Ontology`: {child}",
                    SyntaxWarning,
                    stacklevel=3,
                )

        # return the extracted metadata
        return meta

    def _extract_term(self, elem: etree.Element, aliases: Dict[str, str]):
        """Extract the term from a `owl:Class` element.
        """
        if __debug__:
            if elem.tag != _NS["owl"]["Class"]:
                raise ValueError("expected `owl:Class` element")

        # only create the term if it is not a class by restriction
        iri: Optional[str] = elem.get(_NS["rdf"]["about"])
        if iri is None:
            return None

        # attempt to extract the compact id of the term
        e = elem.find(_NS["oboInOwl"]["id"])
        id_: str = e.text if e is not None and e.text else self._compact_id(iri)

        # get or create the term
        term = (self.ont.get_term if id_ in self.ont else self.ont.create_term)(id_)
        termdata = term._data()
        names: List[str] = []
        comments: List[str] = []

        # extract attributes from annotation of the OWL class
        for child in elem:

            tag: str = child.tag
            attrib: Dict[str, str] = child.attrib
            text: Optional[str] = child.text
            if text is not None and text.isspace():
                text = None

            if tag == _NS["rdfs"]["subClassOf"]:
                if _NS["rdf"]["resource"] in attrib:
                    iri = self._compact_id(attrib[_NS["rdf"]["resource"]])
                    termdata.relationships.setdefault("is_a", set()).add(iri)
                else:
                    pass  # TODO: relationships
            elif tag == _NS["oboInOwl"]["inSubset"]:
                iri = attrib.get(_NS["rdf"]["resource"], text)
                if iri is not None:
                    termdata.subsets.add(self._compact_id(iri))
                else:
                    warnings.warn(
                        f"could not extract subset value in {id_!r}",
                        SyntaxWarning,
                        stacklevel=2,
                    )
            elif tag == _NS["rdfs"]["comment"] and text is not None:
                comments.append(text)
            elif tag in (_NS["oboInOwl"]["created_by"], _NS["dc"]["creator"]):
                termdata.created_by = text
            elif tag in (_NS["oboInOwl"]["creation_date"], _NS["dc"]["date"]):
                termdata.creation_date = dateutil.parser.parse(typing.cast(str, text))
            elif tag == _NS["oboInOwl"]["hasOBONamespace"]:
                if text != self.ont.metadata.default_namespace:
                    termdata.namespace = text
            elif tag == _NS["rdfs"]["label"]:
                if text is not None:
                    names.append(text)
                else:
                    warnings.warn(
                        f"`rdfs:label` without text literal in {id!r}",
                        SyntaxWarning,
                        stacklevel=2,
                    )
            elif tag == _NS["obo"]["IAO_0000115"] and text is not None:
                termdata.definition = Definition(text)
            elif tag in _SYNONYMS_ATTRIBUTES:
                scope = _SYNONYMS_ATTRIBUTES[tag]
                description = attrib.get(_NS["rdf"]["resource"], text)
                if description is not None:
                    termdata.synonyms.add(SynonymData(description, scope))
                else:
                    warnings.warn(
                        f"could not extract synonym value in {id_!r}",
                        SyntaxWarning,
                        stacklevel=3,
                    )
            elif tag == _NS["owl"]["equivalentClass"] and text is not None:
                termdata.equivalent_to.add(self._compact_id(text))
            elif tag == _NS["owl"]["deprecated"]:
                termdata.obsolete = text == "true"
            elif tag == _NS["oboInOwl"]["hasDbXref"]:
                try:
                    if text is not None:
                        termdata.xrefs.add(Xref(text))
                    else:
                        termdata.xrefs.add(Xref(attrib[_NS["rdf"]["resource"]]))
                except ValueError:
                    pass
            elif tag == _NS["oboInOwl"]["hasAlternativeId"]:
                if _NS["rdf"]["resource"] in attrib:
                    iri = self._compact_id(attrib[_NS["rdf"]["resource"]])
                else:
                    iri = self._compact_id(text)
                termdata.alternate_ids.add(iri)
            elif tag == _NS["owl"]["disjointWith"]:
                if _NS["rdf"]["resource"] in attrib:
                    iri = attrib[_NS["rdf"]["resource"]]
                    termdata.disjoint_from.add(self._compact_id(iri))
                else:
                    warnings.warn(
                        "`owl:disjointWith` element without `rdf:resource`",
                        SyntaxWarning,
                        stacklevel=2,
                    )
            elif tag == _NS["obo"]["IAO_0100001"]:
                if _NS["rdf"]["resource"] in attrib:
                    iri = attrib[_NS["rdf"]["resource"]]
                    termdata.replaced_by.add(self._compact_id(iri))
                elif _NS["rdf"]["datatype"] in attrib:
                    termdata.replaced_by.add(self._compact_id(text))
                else:
                    warnings.warn(
                        "could not extract ID from `IAO:0100001` annotation",
                        SyntaxWarning,
                        stacklevel=2,
                    )
            elif tag == _NS["oboInOwl"]["consider"]:
                if _NS["rdf"]["resource"] in attrib:
                    iri = attrib[_NS["rdf"]["resource"]]
                    termdata.consider.add(self._compact_id(iri))
                elif _NS["rdf"]["datatype"] in attrib:
                    termdata.consider.add(self._compact_id(text))
                else:
                    warnings.warn(
                        "could not extract ID from `oboInOwl:consider` annotation",
                        SyntaxWarning,
                        stacklevel=3,
                    )
            elif tag != _NS["oboInOwl"]["id"]:
                if _NS["rdf"]["resource"] in attrib:
                    termdata.annotations.add(self._extract_resource_pv(child))
                elif _NS["rdf"]["datatype"] in attrib and text is not None:
                    termdata.annotations.add(self._extract_literal_pv(child))
                else:
                    warnings.warn(
                        f"unknown element in `owl:Class`: {child.tag}",
                        SyntaxWarning,
                        stacklevel=2,
                    )

        # Owl to OBO post processing:
        # see http://owlcollab.github.io/oboformat/doc/obo-syntax.html#5.11
        # check we got a single name, or select an arbitrary one
        if names:
            if len(names) > 1:
                warnings.warn(
                    f"several names found for {id_!r}, using {names[0]!r}",
                    SyntaxWarning,
                    stacklevel=2,
                )
            termdata.name = names[0]
        # check we got a single comment, or concatenate comments
        if comments:
            if len(comments) > 1:
                warnings.warn(
                    f"several names found for {id_!r}, concatenating",
                    SyntaxWarning,
                    stacklevel=2,
                )
            termdata.comment = "\n".join(comments)

    def _extract_object_property(self, elem: etree.Element, aliases: Dict[str, str]):
        """Extract the object property from an `owl:ObjectProperty` element.
        """
        if __debug__:
            if elem.tag != _NS["owl"]["ObjectProperty"]:
                raise ValueError("expected `owl:ObjectProperty` element")

        # only create the term if it is not a restriction
        iri: Optional[str] = elem.get(_NS["rdf"]["about"])
        if iri is None:  # ignore
            return None

        # attempt to extract the compact id of the term
        elem_id = elem.find(_NS["oboInOwl"]["id"])
        elem_sh = elem.find(_NS["oboInOwl"]["shorthand"])
        if elem_sh is not None and elem_sh.text is not None:
            id_ = aliases[iri] = elem_sh.text
        elif elem_id is not None and elem_id.text is not None:
            id_ = aliases[iri] = elem_id.text
        else:
            id_ = self._compact_id(iri)

        # create the relationship
        rel = (
            self.ont.get_relationship
            if id_ in self.ont
            else self.ont.create_relationship
        )(id_)
        reldata = rel._data()
        names: List[str] = []
        comments: List[str] = []

        # extract attributes from annotation of the OWL relationship
        for child in elem:

            tag: str = child.tag
            attrib: Dict[str, str] = child.attrib
            text: Optional[str] = child.text
            if text is not None and text.isspace():
                text = None

            if tag == _NS["rdfs"]["subObjectPropertyOf"]:
                if _NS["rdf"]["resource"] in attrib:
                    iri = self._compact_id(attrib[_NS["rdf"]["resource"]])
                    reldata.relationships.setdefault("is_a", set()).add(iri)
                else:
                    pass  # TODO: subclassing relationship for relationship
            elif tag == _NS["oboInOwl"]["inSubset"]:
                resource = child.get(_NS["rdf"]["resource"])
                about = child.get(_NS["rdf"]["about"])
                if resource or about:
                    reldata.subsets.add(self._compact_id(resource or about))
                else:
                    warnings.warn(
                        f"could not extract subset in {id_!r}",
                        SyntaxWarning,
                        stacklevel=3,
                    )
            elif tag == _NS["rdf"]["type"]:
                resource = child.get(_NS["rdf"]["resource"])
                if resource == _NS["owl"].raw("TransitiveProperty"):
                    reldata.transitive = True
                elif resource == _NS["owl"].raw("ReflexiveProperty"):
                    reldata.reflexive = True
                elif resource == _NS["owl"].raw("SymmetricProperty"):
                    reldata.symmetric = True
                elif resource == _NS["owl"].raw("AsymmetricProperty"):
                    reldata.asymmetric = True
                elif resource == _NS["owl"].raw("FunctionalProperty"):
                    reldata.functional = True
                elif resource == _NS["owl"].raw("InverseFunctionalProperty"):
                    reldata.inverse_functional = True
            elif tag == _NS["rdfs"]["comment"] and text is not None:
                comments.append(text)
            elif tag in (_NS["oboInOwl"]["created_by"], _NS["dc"]["creator"]):
                reldata.created_by = text
            elif (
                tag == _NS["oboInOwl"]["creation_date"] or tag == _NS["dc"]["date"]
            ) and text is not None:
                reldata.creation_date = dateutil.parser.parse(text)
            elif tag == _NS["oboInOwl"]["hasOBONamespace"]:
                if text != self.ont.metadata.default_namespace:
                    reldata.namespace = text
            elif tag == _NS["rdfs"]["label"]:
                if text is not None:
                    names.append(text)
                else:
                    warnings.warn(
                        f"`rdfs:label` without text literal in {id!r}",
                        SyntaxWarning,
                        stacklevel=2,
                    )
            elif tag == _NS["rdfs"]["domain"] and _NS["rdf"]["resource"] in attrib:
                reldata.domain = self._compact_id(child.attrib[_NS["rdf"]["resource"]])
            elif tag == _NS["rdfs"]["range"] and _NS["rdf"]["resource"] in attrib:
                reldata.range = self._compact_id(child.attrib[_NS["rdf"]["resource"]])
            elif tag == _NS["obo"]["IAO_0000115"] and text is not None:
                reldata.definition = Definition(text)
            elif tag in _SYNONYMS_ATTRIBUTES:
                scope = _SYNONYMS_ATTRIBUTES[tag]
                description = child.get(_NS["rdf"]["resource"], text)
                reldata.synonyms.add(SynonymData(description, scope))
            elif tag == _NS["oboInOwl"]["is_cyclic"] and text is not None:
                reldata.cyclic = text == "true"
            elif tag == _NS["obo"]["IAO_0000427"] and text is not None:
                reldata.antisymmetric = text == "true"
            elif tag == _NS["owl"]["equivalentClass"] and text is not None:
                reldata.equivalent_to.add(self._compact_id(text))
            elif tag == _NS["owl"]["deprecated"]:
                reldata.obsolete = text == "true"
            elif tag == _NS["oboInOwl"]["hasDbXref"]:
                if text is not None:
                    reldata.xrefs.add(Xref(text))
                else:
                    reldata.xrefs.add(Xref(attrib[_NS["rdf"]["resource"]]))
            elif tag == _NS["oboInOwl"]["hasAlternativeId"] and text is not None:
                reldata.alternate_ids.add(text)
            elif tag == _NS["obo"]["IAO_0100001"]:
                if _NS["rdf"]["resource"] in attrib:
                    iri = attrib[_NS["rdf"]["resource"]]
                    reldata.replaced_by.add(self._compact_id(iri))
                elif _NS["rdf"]["datatype"] in attrib:
                    reldata.replaced_by.add(self._compact_id(text))
                else:
                    warnings.warn(
                        "could not extract ID from IAO:0100001 annotation",
                        SyntaxWarning,
                        stacklevel=3,
                    )
            elif tag == _NS["oboInOwl"]["consider"]:
                if _NS["rdf"]["resource"] in attrib:
                    iri = attrib[_NS["rdf"]["resource"]]
                    reldata.consider.add(self._compact_id(iri))
                elif _NS["rdf"]["datatype"] in attrib:
                    reldata.consider.add(self._compact_id(text))
                else:
                    warnings.warn(
                        "could not extract ID from `oboInOwl:consider` annotation",
                        SyntaxWarning,
                        stacklevel=2,
                    )
            elif tag not in (_NS["oboInOwl"]["id"], _NS["oboInOwl"]["shorthand"]):
                if _NS["rdf"]["resource"] in attrib:
                    reldata.annotations.add(self._extract_resource_pv(child))
                elif _NS["rdf"]["datatype"] in attrib and text is not None:
                    reldata.annotations.add(self._extract_literal_pv(child))
                else:
                    warnings.warn(
                        f"unknown element in `owl:ObjectProperty`: {child}",
                        SyntaxWarning,
                        stacklevel=2,
                    )

        # Owl to OBO post processing:
        # see http://owlcollab.github.io/oboformat/doc/obo-syntax.html#5.11
        # check we got a single name, or select an arbitrary one
        if names:
            if len(names) > 1:
                warnings.warn(
                    f"several names found for {id_!r}, using {names[0]!r}",
                    SyntaxWarning,
                    stacklevel=2,
                )
            reldata.name = names[0]
        # check we got a single comment, or concatenate comments
        if comments:
            if len(comments) > 1:
                warnings.warn(
                    f"several names found for {id_!r}, concatenating",
                    SyntaxWarning,
                    stacklevel=3,
                )
            reldata.comment = "\n".join(comments)

        return rel

    def _extract_annotation_property(
        self, elem: etree.Element, aliases: Dict[str, str]
    ):
        if __debug__:
            if elem.tag != _NS["owl"]["AnnotationProperty"]:
                raise ValueError("expected `owl:ObjectProperty` element")

        # special handling of `synonymtypedef` and `subsetdef`
        sub = elem.find(_NS["rdfs"]["subPropertyOf"])
        if sub is not None:
            resource = sub.get(_NS["rdf"]["resource"])
            if resource == _NS["oboInOwl"].raw("SynonymTypeProperty"):
                # extract ID and label of the synonymtypedef
                id_ = self._compact_id(elem.attrib[_NS["rdf"]["about"]])
                label = elem.find(_NS["rdfs"]["label"]).text
                # extract scope if any
                elem_scope = elem.find(_NS["oboInOwl"]["hasScope"])
                if elem_scope is not None:
                    scope = _SYNONYMS.get(elem_scope.attrib[_NS["rdf"]["resource"]])
                else:
                    scope = None
                # add the synonymtypedef to the metadata
                self.ont.metadata.synonymtypedefs.add(SynonymType(id_, label, scope))
                return
            elif resource == _NS["oboInOwl"].raw("SubsetProperty"):
                id_ = self._compact_id(elem.attrib[_NS["rdf"]["about"]])
                elem_comment = elem.find(_NS["rdfs"]["comment"])
                desc = elem_comment.text if elem_comment is not None else None
                self.ont.metadata.subsetdefs.add(Subset(id_, desc or ""))
                return

        # TODO: actual annotation properties
        warnings.warn(
            "cannot process plain `owl:AnnotationProperty`",
            NotImplementedWarning,
            stacklevel=3,
        )

    def _process_axiom(self, elem: etree.Element, aliases: Dict[str, str]):
        # get the source, property and target of the axiom.
        elem_source = elem.find(_NS["owl"]["annotatedSource"])
        elem_property = elem.find(_NS["owl"]["annotatedProperty"])
        elem_target = elem.find(_NS["owl"]["annotatedTarget"])

        # assert source, target and property have a `rdf:resource` attribute.
        for elem in (elem_source, elem_property, elem_target):
            if elem is None or _NS["rdf"]["resource"] not in elem.attrib:
                return

        # check among known properties
        property = elem_property.attrib[_NS["rdf"]["resource"]]
        if property == _NS["obo"].raw("IAO_0000115") and elem_target.text is not None:
            iri = elem_source.attrib[_NS["rdf"]["resource"]]
            resource = aliases.get(iri, iri)
            entity: Entity = self.ont[self._compact_id(resource)]

            entity.definition = d = Definition(elem_target.text)
            for child in elem.iterfind(_NS["oboInOwl"]["hasDbXref"]):
                if child.text is not None:
                    try:
                        d.xrefs.add(Xref(child.text))
                    except ValueError:
                        warnings.warn(
                            f"could not parse Xref: {child.text!r}",
                            SyntaxWarning,
                            stacklevel=3,
                        )
                elif _NS["rdf"]["resource"] in child.attrib:
                    d.xrefs.add(Xref(child.get(_NS["rdf"]["resource"])))
                else:
                    warnings.warn(
                        "`oboInOwl:hasDbXref` element has no text",
                        SyntaxWarning,
                        stacklevel=3,
                    )

        elif (
            property == _NS["oboInOwl"].raw("hasDbXref")
            and elem_target.text is not None
        ):
            iri = elem_source.attrib[_NS["rdf"]["resource"]]
            resource = aliases.get(iri, iri)
            entity = self.ont[self._compact_id(resource)]
            label = elem.find(_NS["rdfs"]["label"])

            if label is not None and label.text is not None:
                entity._data().xrefs.add(Xref(elem_target.text, label.text))
            else:
                entity._data().xrefs.add(Xref(elem_target.text))

        elif property in _SYNONYMS:
            iri = elem_source.attrib[_NS["rdf"]["resource"]]
            resource = aliases.get(iri, iri)
            entity = self.ont[self._compact_id(resource)]
            type_ = elem.find(_NS["oboInOwl"]["hasSynonymType"])

            try:
                synonym = next(
                    s._data()
                    for s in entity.synonyms
                    if s.description == elem_target.text
                    and s.scope == _SYNONYMS[property]
                )
            except StopIteration:
                description = elem_target.get(_NS["rdf"]["resource"], elem_target.text)
                if description is None:
                    warnings.warn(
                        f"could not extract synonym value in {elem!r}",
                        SyntaxWarning,
                        stacklevel=3,
                    )
                    return
                synonym = SynonymData(
                    description,
                    scope=_SYNONYMS[property],
                    type=type_.text if type_ is not None else None,
                )

            entity._data().synonyms.add(typing.cast(SynonymData, synonym))
            for child in elem.iterfind(_NS["oboInOwl"]["hasDbXref"]):
                if child.text is not None:
                    synonym.xrefs.add(Xref(child.text))
                else:
                    warnings.warn(
                        "`oboInOwl:hasDbXref` element has no text",
                        SyntaxWarning,
                        stacklevel=3,
                    )

        else:
            warnings.warn(
                f"unknown axiom property: {property!r}", SyntaxWarning, stacklevel=3,
            )
