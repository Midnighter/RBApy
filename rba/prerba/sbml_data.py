"""Module defining SbmlData class."""

# python 2/3 compatibility
from __future__ import division, print_function, absolute_import

# global imports
import copy
import itertools
import re
import libsbml
import sympy
from sympy.logic.boolalg import to_dnf

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
    external_prefixes: list
        Prefix of external metabolites (e.g. M_glc for M_glc_e).

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
        model = document.getModel()
        self._initialize_species(model, external_ids)
        self.external_prefixes = [self._prefix(m.id)
                                  for m in self.species
                                  if m.boundary_condition]
        self._extract_reactions_and_enzymes(model, cytosol_id)

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
        for spec in model.getListOfSpecies():
            boundary = spec.getBoundaryCondition()
            if spec.getCompartment() in external_ids:
                boundary = True
            self.species.append(rba.xml.Species(spec.getId(), boundary))

    def _identify_external_compartments(self, model):
        # Compartments are considered external if all metabolites
        # they contain participate in a sink/production reaction
        sink_species = self._sink_species(model.getListOfReactions())
        result = set(c.getId() for c in model.getListOfCompartments())
        for metabolite in model.getListOfSpecies():
            if metabolite.getId() not in sink_species:
                result.discard(metabolite.getCompartment())
        return result

    def _sink_species(self, reactions):
        result = []
        for reaction in reactions:
            if (len(reaction.getListOfReactants()) == 1 and
                    len(reaction.getListOfProducts()) == 0):
                result.append(reaction.getReactant(0).getSpecies())
            elif (len(reaction.getListOfProducts()) == 1 and
                    len(reaction.getListOfReactants()) == 0):
                result.append(reaction.getProduct(0).getSpecies())
        return set(result)

    def _extract_reactions_and_enzymes(self, model, cytosol_id):
        self.reactions = rba.xml.ListOfReactions()
        self.enzymes = []
        parser = self._create_annotation_parser(model)
        for reaction in model.getListOfReactions():
            try:
                enzymes = parser.enzyme_composition(reaction)
            except UserWarning:
                self._print_invalid_enzyme_notes()
                raise UserWarning('Invalid SBML.')
            # if no enzyme was detected, add "empty" enzyme
            if not enzymes:
                enzymes = [['']]
            # we create one reaction per associated enzyme
            for suffix, enzyme in enumerate(enzymes):
                id_ = reaction.getId()
                if suffix > 0:
                    id_ += '_duplicate_' + str(suffix+1)
                new_reaction = self._create_reaction(id_, reaction)
                self.reactions.append(new_reaction)
                self.enzymes.append(self._create_enzyme(
                    new_reaction, enzyme, cytosol_id
                ))

    def _create_annotation_parser(self, model):
        if model.getPlugin('fbc'):
            return FbcAnnotationParser(model.getPlugin('fbc'))
        else:
            return CobraNoteParser()

    def _print_invalid_enzyme_notes(self):
        print('Your SBML file does not contain fbc gene products nor uses '
              ' COBRA notes to define enzyme composition for every '
              'reaction. Please comply with SBML'
              ' requirements defined in the README and rerun script.')

    def _create_reaction(self, id_, reaction):
        result = rba.xml.Reaction(id_, reaction.getReversible())
        for r in reaction.getListOfReactants():
            result.reactants.append(
                rba.xml.SpeciesReference(r.getSpecies(), r.getStoichiometry())
            )
        for p in reaction.getListOfProducts():
            result.products.append(
                rba.xml.SpeciesReference(p.getSpecies(), p.getStoichiometry())
            )
        return result

    def _create_enzyme(self, reaction, composition, cytosol_id):
        enzyme = Enzyme(reaction.id,
                        not self._all_species_in_same_compartment(reaction))
        enzyme.gene_association = composition
        enzyme.imported_metabolites = self._imported_metabolites(
            reaction, cytosol_id
        )
        enzyme.initialize_efficiencies()
        return enzyme

    def _all_species_in_same_compartment(self, reaction):
        compartments = [self._suffix(m.species)
                        for m in itertools.chain(reaction.reactants,
                                                 reaction.products)]
        return all(c == compartments[0] for c in compartments[1:])

    def _imported_metabolites(self, reaction, cytosol_id):
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
                reaction, cytosol_id
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

    def _noncytosolic_external_reactants(self, reaction, cytosol_id):
        result = []
        for reactant in reaction.reactants:
            prefix, cpt = reactant.species.rsplit('_', 1)
            if cpt != cytosol_id and prefix in self.external_prefixes:
                result.append(reactant.species)
        return result


class FbcAnnotationParser(object):
    """Parse fbc annotation to gather enzyme compositions."""
    def __init__(self, fbc_model):
        self._gene_names = {}
        for gene_product in fbc_model.getListOfGeneProducts():
            self._gene_names[gene_product.getId()] = gene_product.getLabel()
        # remove 'G_' prefix if present
        # (compatibility issue with COBRApy,
        #  the label should be the gene name according to FBC specs)
        self._gene_names = {id: re.sub("^G_", "", name)
                            for id, name in self._gene_names.items()}

    def enzyme_composition(self, reaction):
        gp_association = reaction.getPlugin('fbc') \
                                 .getGeneProductAssociation()
        if gp_association:
            result = self._parse_fbc_association(gp_association.getAssociation())
            return extract_enzyme_list(result)
        else:
            return [[]]

    def _parse_fbc_association(self, association):
        if association.isFbcOr():
            result = " | ".join(self._parse_fbc_association(a)
                                for a in association.getListOfAssociations())
            return "(" + result + ")"
        elif association.isFbcAnd():
            result = " & ".join(self._parse_fbc_association(a)
                                for a in association.getListOfAssociations())
            return "(" + result + ")"
        elif association.isGeneProductRef():
            gene_id = association.getGeneProduct()
            return self._gene_names[gene_id]
        else:
            print('Unknown FBC association type')
            raise UserWarning('Invalid SBML.')


class CobraNoteParser(object):
    def enzyme_composition(self, reaction):
        result = []
        if not reaction.getNotes():
            raise UserWarning('Missing enzyme annotation')
        for ga in self._gene_associations(reaction.getNotes()):
            sympy_expr = self._parse_gene_association(ga)
            composition = extract_enzyme_list(sympy_expr)
            if composition:
                result += composition
        return result

    def _gene_associations(self, note):
        # fields may be encapsulated in a <html> tag (or equivalent)
        note = self._remove_html_tag(note)
        return (note.getChild(i).getChild(0).toString()
                for i in range(note.getNumChildren()))

    def _remove_html_tag(self, note):
        if (note.getNumChildren() == 1
                and note.getChild(0).getName() != "p"):
            return note.getChild(0)
        return note

    def _parse_gene_association(self, text):
        tags = text.split(':', 1)
        if len(tags) != 2 or tags[0] != "GENE_ASSOCIATION":
            return None
        sympy_expr = re.sub(r'\bOR\b', r'|', tags[1], flags=re.IGNORECASE)
        sympy_expr = re.sub(r'\bAND\b', r'&', sympy_expr, flags=re.IGNORECASE)
        return(sympy_expr)

def extract_enzyme_list(sympy_expr):
    dnf_expr = str(to_dnf(sympy_expr))
    # remove parentheses for easier parsing (not useful once in DNF)
    enzyme_description = re.sub(r'[()]', r'', dnf_expr)
    if not enzyme_description:
        return []
    return [enzyme_composition(e)
            for e in enzyme_description.split(' | ')]

def enzyme_composition(enzyme):
    return [gene.strip() for gene in enzyme.split(' & ')]
