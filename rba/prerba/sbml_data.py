"""Module defining SbmlData class."""

# python 2/3 compatibility
from __future__ import division, print_function, absolute_import

# global imports
import copy
import itertools
import libsbml

# local imports
from rba.prerba.enzyme import Enzyme
import rba.xml


class SbmlData(object):
    """
    Class used to parse RBA-relevant SBML data.

    Attributes
    ----------
    species: rba.xml.ListOfSpecies
        SBML species.
    enzymes: list of rba.prerba.enzyme.Enzyme
        Enzymes corresponding to SBML annotations.
    reactions: rba.xml.ListOfReaction
        SBML reactions.
    external_metabolites: list
        SBML identifiers of external metabolites.

    """

    def __init__(self, input_file, cytosol_id='c', external_ids=None):
        """
        Build from file.

        Parameters
        ----------
        input: str
            Path to input file.
        cytosol_id: str
            identifier of cytosol in the SBML file.
        external_ids: list of str
            identifiers of external compartments in SBML file.

        """
        # WARNING: not storing document in a variable will result
        # in segmentation fault!
        document = self._load_document(input_file)
        model = document.model
        self._initialize_species(model, external_ids)
        self.external_metabolites = [m.id for m in self.species
                                     if m.boundary_condition]
        self._enzyme_comp = self._extract_enzyme_composition(model)
        self.reactions = self._extract_reactions(model)
        self._duplicate_reactions_with_multiple_enzymes()
        self._initialize_enzymes(cytosol_id)

    def _load_document(self, input_file):
        document = libsbml.readSBML(input_file)
        if document.getNumErrors() > 0:
            document.printErrors()
            raise UserWarning('Invalid SBML.')
        return document

    def _initialize_species(self, model, external_ids):
        if external_ids is None:
            external_ids = []
        external_ids += self._identify_external_compartments(model)
        self.species = rba.xml.ListOfSpecies()
        for spec in model.species:
            boundary = spec.boundary_condition
            if spec.compartment in external_ids:
                boundary = True
            self.species.append(rba.xml.Species(spec.getId(), boundary))

    def _identify_external_compartments(self, model):
        # Compartments are considered external if all metabolites
        # they contain participate in a sink/production reaction
        sink_species = self._sink_species(model.reactions)
        result = set(c.id for c in model.compartments)
        for metabolite in model.species:
            if metabolite.id not in sink_species:
                result.discard(metabolite.compartment)
        return result

    def _sink_species(self, reactions):
        result = []
        for reaction in reactions:
            if (len(reaction.reactants) == 1 and
                    len(reaction.products) == 0):
                result.append(reaction.reactants[0].species)
            elif (len(reaction.products) == 1 and
                    len(reaction.reactants) == 0):
                result.append(reaction.products[0].species)
        return set(result)

    def _extract_enzyme_composition(self, model):
        result = FbcAnnotationParser(model).parse_enzymes()
        if not result:
            result = CobraNoteParser(model).read_notes()
        if not result:
            print('Your SBML file does not contain fbc gene products nor uses '
                  ' COBRA notes to define enzyme composition. '
                  'Please comply with SBML'
                  ' requirements defined in the README and rerun script.')
            raise UserWarning('Invalid SBML.')
        return result

    def _extract_reactions(self, model):
        result = rba.xml.ListOfReactions()
        for reaction in model.reactions:
            new_reaction = rba.xml.Reaction(reaction.id, reaction.reversible)
            for reactant in reaction.reactants:
                new_reaction.reactants.append(
                    rba.xml.SpeciesReference(reactant.species,
                                             reactant.stoichiometry)
                )
            for product in reaction.products:
                new_reaction.products.append(
                    rba.xml.SpeciesReference(product.species,
                                             product.stoichiometry)
                )
            result.append(new_reaction)
        return result

    def _duplicate_reactions_with_multiple_enzymes(self):
        new_enzymes = []
        new_reactions = rba.xml.ListOfReactions()
        for reaction, enzymes in zip(self.reactions, self._enzyme_comp):
            suffix = 0
            for enzyme in enzymes:
                suffix += 1
                r_clone = copy.copy(reaction)
                if suffix > 1:
                    r_clone.id += '_' + str(suffix)
                new_enzymes.append(enzyme)
                new_reactions.append(r_clone)
        self.reactions = new_reactions
        self._enzyme_comp = new_enzymes

    def _initialize_enzymes(self, cytosol_id):
        self.enzymes = []
        external_prefixes = set(
            self._prefix(m) for m in self.external_metabolites
        )
        for r, c in zip(self.reactions, self._enzyme_comp):
            enzyme = Enzyme(r.id, self._has_membrane_enzyme(r))
            enzyme.gene_association = c
            enzyme.imported_metabolites = self._imported_metabolites(
                r, cytosol_id, external_prefixes
            )
            self.enzymes.append(enzyme)

    def _has_membrane_enzyme(self, reaction):
        compartments = [self._suffix(m.species)
                        for m in itertools.chain(reaction.reactants,
                                                 reaction.products)]
        return any(c != compartments[0] for c in compartments[1:])

    def _imported_metabolites(self, reaction, cytosol_id, external_prefixes):
        """
        Identify external metabolites imported into the cytosol.

        They meet the following conditions:
        - they are a reactant.
        - they have the same prefix (e.g. M_glc) as one of the
        external metabolites.
        - they are not part of the cytosol.
        - one of the products is in the cytosol.
        """
        if self._has_cytosolic_product(reaction, cytosol_id):
            return self._noncytosolic_external_reactants(
                reaction, cytosol_id, external_prefixes
            )
        else:
            return []

    def _prefix(self, metabolite_id):
        return metabolite_id.rsplit('_', 1)[0]

    def _has_cytosolic_product(self, reaction, cytosol_id):
        return any(self._suffix(p.species) == cytosol_id
                   for p in reaction.products)

    def _suffix(self, metabolite_id):
        return metabolite_id.rsplit('_', 1)[1]

    def _noncytosolic_external_reactants(self, reaction, cytosol_id,
                                         external_prefixes):
        result = []
        for reactant in reaction.reactants:
            prefix, cpt = reactant.species.rsplit('_', 1)
            if cpt != cytosol_id and prefix in external_prefixes:
                result.append(reactant.species)
        return result


class FbcAnnotationParser(object):
    """Parse fbc annotation to gather enzyme compositions."""
    def __init__(self, model):
        self._model = model
        self._fbc = model.getPlugin('fbc')

    def parse_enzymes(self):
        if not self._fbc:
            return []
        self._initialize_gene_id_to_name_map()
        return [self._enzyme_composition(r) for r in self._model.reactions]

    def _initialize_gene_id_to_name_map(self):
        self._gene_names = {}
        for gene_product in self._fbc.getListOfGeneProducts():
            self._gene_names[gene_product.id] = gene_product.label

    def _enzyme_composition(self, reaction):
        gp_association = reaction.getPlugin('fbc') \
                                 .getGeneProductAssociation()
        if gp_association:
            return self._read_fbc_association(gp_association)
        else:
            return [[]]

    def _read_fbc_association(self, gp_association):
        """We assume that relations are always 'or's of 'and's."""
        association = gp_association.getAssociation()
        if association.isFbcOr():
            return [self._read_fbc_association_components(a)
                    for a in association.getListOfAssociations()]
        else:
            return [self._read_fbc_association_components(association)]

    def _read_fbc_association_components(self, association):
        """We assume that relations are always 'and's."""
        if association.isGeneProductRef():
            gene_id = association.getGeneProduct()
            return [self._gene_names[gene_id]]
        elif association.isFbcAnd():
            result = []
            for assoc in association.getListOfAssociations():
                result += self._read_fbc_association_components(assoc)
            return result
        else:
            print('Invalid association (we only support ors of ands)')
            raise UserWarning('Invalid SBML.')


class CobraNoteParser(object):
    def __init__(self, model):
        self._model = model

    def read_notes(self):
        reactions = self._model.reactions
        result = []
        for reaction in self._model.reactions:
            if not reaction.notes:
                return []
            result.append(self._parse_note(reaction.notes))
        return result

    def _parse_note(self, note):
        result = []
        for ga in self._gene_associations(note):
            composition = self._parse_gene_association(ga)
            if composition:
                result.append(composition)
        return result

    def _gene_associations(self, note):
        # fields may be encapsulated in a <html> tag (or equivalent)
        note = self._remove_html_tag(note)
        return (note.getChild(i).getChild(0).toString()
                for i in range(notes.getNumChildren()))

    def _remove_html_tag(self, note):
        if (note.getNumChildren() == 1
                and note.getChild(0).getName() != "p"):
            return note.getChild(0)
        return note

    def _parse_gene_association(self, text):
        """We assume that relations are always 'or's of 'and's."""
        tags = text.split(':', 1)
        if len(tags) != 2 and tags[0] != "GENE_ASSOCIATION":
            print('Invalid note field: ' + text)
            return None
        enzyme_description = self._remove_parentheses(tags[1])
        if not enzyme_description:
            return []
        return [self._enzyme_composition(e)
                for e in enzyme_description.split(' or ')]

    def _remove_parentheses(self, string):
        return ''.join(c for c in string if c not in '()')

    def _enzyme_composition(self, enzyme):
        return [gene.strip() for gene in enzyme.split(' and ')]
