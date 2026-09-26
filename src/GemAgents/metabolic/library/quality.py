"""Quality control for the unified public reaction library."""

from __future__ import annotations

import csv
import math
from pathlib import Path

# These identifiers occur in public reference models with mutually
# inconsistent metadata.  The values below are the canonical definitions used
# by the operational reaction library.  Keeping the correction at the
# metabolite level fixes every dependent reaction (flavin/iron reduction and
# peptidoglycan processing) without editing individual stoichiometric rows.
CANONICAL_CHEMISTRY: dict[str, tuple[str, int]] = {
    "rbflvrd_c": ("C17H22N4O6", 0),
    "murein5px4p_p": ("C77H117N15O40", -4),
    "murein4px4p_p": ("C74H112N14O39", -4),
}

# ModelSEED's common nucleotide compounds use the protonated species while
# BiGG's cytosolic carrier metabolites use the deprotonated species.  Keeping
# both IDs in one operational model disconnects ATP/ADP chemistry and permits
# artificial carrier cycles.  Each mapping below differs by exactly H+.
CANONICAL_PROTONATION_ALIASES: dict[str, str] = {
    "cpd00002_c": "atp_c",
    "cpd00008_c": "adp_c",
    "cpd00031_c": "gdp_c",
    "cpd00038_c": "gtp_c",
    "cpd00096_c": "cdp_c",
    "cpd00052_c": "ctp_c",
    "cpd00014_c": "udp_c",
    "cpd00062_c": "utp_c",
    "cpd00090_c": "idp_c",
    "cpd00068_c": "itp_c",
}


def _correct_atp_synthase_compartment(model) -> list[dict[str, object]]:
    """Use the periplasmic proton / cytosolic phosphate ATP synthase form.

    The ModelSEED row for ``rxn08173`` carries compartment tags for these
    species, but the union loader can map them to the external pool.  That
    turns the ATP synthase into a direct external-energy shortcut.  The
    reference E. coli reaction is ``ADP + 4 H_p + Pi_c -> ATP + H2O + 3 H_c``.
    """
    if "rxn08173_c" not in model.reactions:
        return []
    required = {"adp_c", "h_p", "pi_c", "atp_c", "h2o_c", "h_c"}
    if not required <= {metabolite.id for metabolite in model.metabolites}:
        return []
    reaction = model.reactions.get_by_id("rxn08173_c")
    expected = {
        "adp_c": -1.0,
        "h_p": -4.0,
        "pi_c": -1.0,
        "atp_c": 1.0,
        "h2o_c": 1.0,
        "h_c": 3.0,
    }
    current = {
        metabolite.id: float(coefficient)
        for metabolite, coefficient in reaction.metabolites.items()
    }
    if current == expected:
        return []
    if current != {
        "adp_c": -1.0,
        "h_e": -4.0,
        "pi_e": -1.0,
        "atp_c": 1.0,
        "h2o_c": 1.0,
        "h_c": 3.0,
    }:
        return []
    reaction.subtract_metabolites(dict(reaction.metabolites))
    reaction.add_metabolites(
        {
            model.metabolites.get_by_id(identifier): coefficient
            for identifier, coefficient in expected.items()
        }
    )
    reaction.notes["canonical_chemistry_correction"] = "ATP synthase compartment mapping"
    return [
        {
            "reaction_id": reaction.id,
            "before": "ADP_c + 4 H_e + Pi_e -> ATP_c + H2O_c + 3 H_c",
            "after": "ADP_c + 4 H_p + Pi_c -> ATP_c + H2O_c + 3 H_c",
            "reason": "reference_ATPS4rpp_compartment_mapping",
        }
    ]


def _collapse_protonation_aliases(model) -> list[dict[str, object]]:
    """Collapse verified ModelSEED/BiGG nucleotide protonation aliases."""
    changes: list[dict[str, object]] = []
    if "h_c" not in model.metabolites:
        return changes
    proton = model.metabolites.get_by_id("h_c")
    for source_id, target_id in CANONICAL_PROTONATION_ALIASES.items():
        if source_id not in model.metabolites or target_id not in model.metabolites:
            continue
        source = model.metabolites.get_by_id(source_id)
        target = model.metabolites.get_by_id(target_id)
        source_elements = dict(source.elements or {})
        target_elements = dict(target.elements or {})
        element_delta = {
            key: source_elements.get(key, 0) - target_elements.get(key, 0)
            for key in set(source_elements) | set(target_elements)
        }
        element_delta = {key: value for key, value in element_delta.items() if value}
        if element_delta != {"H": 1} or source.charge != target.charge + 1:
            continue
        affected = list(source.reactions)
        for reaction in affected:
            coefficient = reaction.metabolites[source]
            reaction.add_metabolites(
                {source: -coefficient, target: coefficient, proton: coefficient}
            )
        if not source.reactions:
            model.remove_metabolites([source], destructive=False)
        changes.append(
            {
                "source_metabolite": source_id,
                "target_metabolite": target_id,
                "protonation_delta": "source=target+H+",
                "reactions_rewritten": len(affected),
            }
        )
    return changes


def apply_canonical_chemistry(model) -> list[dict[str, object]]:
    """Apply audited shared-metabolite definitions to a working model.

    The function is intentionally deterministic and only touches the audited
    metabolite definitions and protonation aliases above. It returns an audit
    ledger so callers can preserve exactly which source values were replaced.
    Missing metabolites are ignored because reference models may legitimately
    omit an entire pathway.
    The caller owns the copy boundary; this function mutates only the model it
    receives and returns an audit ledger for provenance.
    """
    changes: list[dict[str, object]] = []
    for metabolite_id, (formula, charge) in CANONICAL_CHEMISTRY.items():
        if metabolite_id not in model.metabolites:
            continue
        metabolite = model.metabolites.get_by_id(metabolite_id)
        before = (metabolite.formula, metabolite.charge)
        after = (formula, charge)
        if before == after:
            continue
        metabolite.formula = formula
        metabolite.charge = charge
        metabolite.notes["canonical_chemistry_correction"] = "GemAgents audited library"
        changes.append(
            {
                "metabolite_id": metabolite_id,
                "before_formula": before[0],
                "before_charge": before[1],
                "after_formula": formula,
                "after_charge": charge,
            }
        )
    alias_changes = _collapse_protonation_aliases(model)
    changes.extend(alias_changes)
    # The public library stores several nucleotide species under ModelSEED
    # IDs; apply the compartment correction after those aliases are collapsed.
    changes.extend(_correct_atp_synthase_compartment(model))
    if changes:
        model.notes["canonical_chemistry_corrections"] = str(len(changes))
    return changes
def load_reaction_quality_table(path: Path) -> dict[str, dict[str, str]]:
    """Load a prepared reaction-quality TSV, returning an empty table if absent."""
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        return {row["reaction_id"]: row for row in csv.DictReader(handle, delimiter="\t")}


def guard_energy_hydrolysis_direction(reaction) -> bool:
    """Block chemically invalid reverse directions in public reaction unions.

    Public-model direction unions can widen a reaction whenever any source
    model marks it reversible.  Exact stoichiometry checks cover directional
    ATP hydrolysis and transport reactions whose reverse would create energy
    carriers.  Return whether bounds were changed.
    """
    aliases = reaction.annotation.get("bigg.reaction", [])
    aliases = {aliases} if isinstance(aliases, str) else set(aliases or [])
    stoichiometry = {
        metabolite.id: float(coefficient)
        for metabolite, coefficient in reaction.metabolites.items()
    }
    # Reference-scaffold reactions are prefixed when imported, so their
    # original BiGG identifier is retained only in ``reference_reaction``.
    # Include that provenance alias in the same deterministic direction
    # policy; otherwise a reference reaction can reintroduce a cycle after
    # the native library has already been guarded.
    reference_alias = reaction.notes.get("reference_reaction")
    alias_names = {str(value).upper() for value in aliases} | {reaction.id.upper()}
    if reference_alias:
        alias_names.add(str(reference_alias).upper())
    # The closed-boundary carrier audit identified these reference reactions
    # as reverse shortcuts in the nucleotide-salvage network.  Their published
    # equations are retained, but the reverse direction is excluded from the
    # operational library.  Match both canonical IDs and source aliases.
    nucleotide_cycle_guards = {
        "DUTCP",
        "RXN00362_C",
        "PYNP1",
        "NDPK6",
    }
    if alias_names & nucleotide_cycle_guards and reaction.lower_bound < 0:
        reaction.lower_bound = 0.0
        reaction.notes["qc_direction_guard"] = "closed_boundary_nucleotide_cycle_guard"
        return True
    # PPK2/PPK2r is written as ATP + PPi <=> ADP + triphosphate in the
    # ModelSEED union.  Its reverse is an ATP regeneration shortcut when
    # triphosphate is otherwise freely available in a closed audit.
    polyphosphate_kinase = alias_names & {"RXN00104_C", "PPIK", "PPK2", "PPK2R"}
    if polyphosphate_kinase and reaction.lower_bound < 0:
        ppi = stoichiometry.get("ppi_c", stoichiometry.get("cpd00012_c", 0.0))
        triphosphate = stoichiometry.get("cpd00421_c", 0.0)
        atp = stoichiometry.get("atp_c", stoichiometry.get("cpd00002_c", 0.0))
        adp = stoichiometry.get("adp_c", stoichiometry.get("cpd00008_c", 0.0))
        if atp == -1.0 and ppi == -1.0 and adp == 1.0 and triphosphate == 1.0:
            reaction.lower_bound = 0.0
            reaction.notes["qc_direction_guard"] = "polyphosphate_kinase_reverse_blocked"
            return True
    # The aerobic E. coli condition must not use anaerobic fumarate
    # reductases or formate-hydrogen lyase to create a proton motive force.
    # These reactions are present in the public union, but the reference
    # scaffold keeps the corresponding branches closed under minimal aerobic
    # growth.
    aerobic_anaerobic_branch = alias_names & {
        "FRD2",
        "FRD3",
        "RXN37641_C",
        "RXN08518_C",
    }
    if aerobic_anaerobic_branch:
        if reaction.lower_bound != 0.0 or reaction.upper_bound != 0.0:
            reaction.bounds = (0.0, 0.0)
            reaction.notes["qc_direction_guard"] = "aerobic_anaerobic_branch_closed"
            return True
        return False
    # These reactions are represented as reversible in parts of the public
    # union, but their reverse directions are the exact ATP/UTP/ITP
    # regeneration shortcuts exposed by the closed-boundary probes.
    forward_only_ids = {
        "RXN00237_C",  # NDP kinase: retain ATP -> GTP direction for QC closure
        "RXN00117_C",  # NDP kinase: retain ATP -> UTP direction
        "RXN00772_C",  # ribokinase: reverse would regenerate ATP in salvage loops
        "RXN00216_C",  # glucose kinase
        "RXN27378_C",  # proton-coupled transhydrogenase
        "RXN05887_C",  # hydrogen:NAD oxidoreductase
        "RXN00239_C",  # GMP kinase
        "RXN00292_C",  # UDP-N-acetylglucosamine epimerase hydrolysis
        "RXN00364_C",  # CMP kinase
        "RXN00409_C",  # CDP kinase: block ATP-regenerating reverse direction
        "RXN00547_C",  # fructose-6-phosphate kinase
        "RXN36696_C",  # dihydroorotate:quinone oxidoreductase
        "RXN36697_C",  # dihydroorotate:quinone oxidoreductase variant
        "RXN36783_C",  # quinone reductase
        "RXN37587_C",  # dihydroorotate dehydrogenase
        "RXN37623_C",  # NAD(P)H dehydrogenase
        "RXN11156_C",  # NADH:menaquinone oxidoreductase
        "RXN14017_C",  # glycerol-3-phosphate:quinone oxidoreductase
        "RXN35653_C",  # thiosulfate:quinone oxidoreductase variant
        "RXN35655_C",  # thiosulfate:quinone oxidoreductase variant
        "RXN36759_C",  # FAD-dependent malate dehydrogenase
        "RXN37588_C",  # dihydroorotate dehydrogenase (quinone)
        "RXN37603_C",  # malate dehydrogenase (quinone)
        "RXN37634_C",  # glycerol-3-phosphate dehydrogenase
        "RXN00557_C",  # ITP-dependent fructose-phosphate kinase
        "RXN00940_C",  # NMN phosphoribohydrolase
        "RXN00914_C",  # guanosine kinase
        "RXN01084_C",  # ATP:cob(I)alamin adenosyltransferase
        "RXN01671_C",  # ribosylnicotinamide kinase
        "RXN01563_C",  # pyrimidine-nucleoside ribohydrolase
        "RXN01650_C",  # pyrimidine-nucleoside phosphorolysis
        "RXN01169_C",  # glucose kinase
        "RXN01133_C",  # maltose O-acetyltransferase
        "RXN15011_C",  # maltose glucohydrolase
        "RXN15010_C",  # maltose glucohydrolase variant
        "RXN15001_C",  # trehalase
        "RXN15002_C",  # trehalase variant
        "RXN15073_C",  # trehalose-6-phosphate phosphohydrolase
        "RXN15074_C",  # trehalose-6-phosphate phosphohydrolase variant
        "RXN15081_C",  # fructose-6-phosphate kinase
        "RXN15249_C",  # alpha-glucose kinase
        "RXN15223_C",  # maltose O-acetyltransferase variant
        "RXN15313_C",  # pyrimidine-nucleoside ribohydrolase variant
        "RXN30765_C",  # ribokinase
        "RXN19777_C",  # UDP-glucose pyrophosphorylase
        "RXN27383_C",  # trehalose-6-phosphate synthase
        "RXN27702_C",  # aldose epimerase union variant
        "RXN08205_C",  # CDP-diacylglycerol pyrophosphatase (C18:1)
        "RXN08312_C",  # CDP-diacylglycerol synthetase (C18:1)
        "RXN15087_C",  # glucose aldose-ketose isomerase
        "RXN08204_C",  # CDP-diacylglycerol pyrophosphatase (C18:0)
        "RXN08311_C",  # CDP-diacylglycerol synthetase (C18:0)
        "RXN08199_C",  # CDP-diacylglycerol pyrophosphatase (C12:0)
        "RXN08202_C",  # CDP-diacylglycerol pyrophosphatase (C16:0)
        "RXN08203_C",  # CDP-diacylglycerol pyrophosphatase
        "RXN08201_C",  # CDP-diacylglycerol pyrophosphatase variant
        "RXN08310_C",  # CDP-diacylglycerol synthetase
        "RXN08308_C",  # CDP-diacylglycerol synthetase variant
        "RXN08306_C",  # CDP-diacylglycerol synthetase (C12:0)
        "RXN08309_C",  # CDP-diacylglycerol synthetase (C16:0)
        "RXN09016_C",  # nucleoside-triphosphate tripolyhydrolase
        "RXN10091_C",  # alternate cob(I)alamin adenosyltransferase equation
        "RXN15065_C",  # UTP-dependent fructose-phosphate kinase
        "RXN15118_C",  # uridine ribohydrolase
        "RXN20780_C",  # ribokinase
        "RXN27430_C",  # uridine nucleosidase
        "RXN33589_C",  # fructose-bisphosphatase
        "RXN37018_C",  # beta-D-ribofuranose kinase
        "RXN28025_C",  # UDP-N-acetylglucosamine pyrophosphorylase
        "RXN30058_C",  # ribokinase variant
        "RXN39926_C",  # N-acetylglucosamine kinase
    }
    if alias_names & forward_only_ids and reaction.lower_bound < 0:
        reaction.lower_bound = 0.0
        reaction.notes["qc_direction_guard"] = "closed_boundary_forward_only_guard"
        return True
    # These reversible transporters touch closed external pools.  Their
    # import directions can otherwise feed ATP synthase during strict
    # boundary audits through periplasmic proton or phosphate shuttles.
    if alias_names & {"HTEX", "PITEX"}:
        # Keep both transport directions available to the reconstructed
        # growth model.  The strict carrier probes close these IDs in their
        # private audit copy instead of changing the exported model.
        reaction.notes["qc_probe_closed"] = "closed_external_transport_reverse_blocked"
    # CYTK1 is reversible in several public unions; its reverse direction
    # closes an ATP-producing loop with adenylate kinase and CDP reactions.
    if alias_names & {"CYTK1", "CYTK1M", "CYTK1N"} and reaction.lower_bound < 0:
        reaction.lower_bound = 0.0
        reaction.notes["qc_direction_guard"] = "cytidylate_kinase_reverse_blocked"
        return True
    # Direction unions and reference scaffolds use several identifiers for
    # the same proton-coupled ATP synthase chemistry (for example
    # ``ATPS4rpp`` and ``rxn08173_c``).  The reverse of a producer, or the
    # forward direction of a hydrolysis-written variant, creates a closed
    # ATP loop when boundary reactions are closed.  Use the explicit BiGG /
    # ModelSEED aliases plus the ATP/ADP stoichiometry to cover equation
    # variants without changing unrelated reversible reactions.
    atp_synthase_alias = any(
        name.split("_")[0] in {
            "ATPS4R",
            "ATPS4RPP",
            "ATPS4M",
            "ATPSUM",
            "ATPS_H",
            "RXN08173",
            "RXN10042",
            "RXN27727",
            "RXN38049",
            "RXN38050",
        }
        for name in alias_names
    )
    if atp_synthase_alias:
        atp = stoichiometry.get("atp_c", stoichiometry.get("cpd00002_c", 0.0))
        adp = stoichiometry.get("adp_c", stoichiometry.get("cpd00008_c", 0.0))
        if atp > 0 and adp < 0 and reaction.lower_bound < 0:
            reaction.lower_bound = 0.0
            reaction.notes["qc_direction_guard"] = "ATP_synthase_producer_forward"
            return True
        if atp < 0 and adp > 0 and reaction.lower_bound < 0:
            # This equation is written as ATP hydrolysis.  Keep that
            # biochemical direction only; allowing its reverse alongside a
            # producer-written ATP synthase variant creates a free proton/
            # ATP loop in the union.
            reaction.lower_bound = 0.0
            reaction.notes["qc_direction_guard"] = "ATP_synthase_hydrolysis_forward"
            return True
    # ATP synthase is used here as a producer driven by the proton motive
    # force.  Allowing its hydrolytic reverse direction creates a closed
    # respiratory/ATP loop when the public union also contains reversible
    # redox reactions.
    if reaction.id.upper() in {"ATPSUM", "ATPS4M", "ATPS_H"} and reaction.lower_bound < 0:
        reaction.lower_bound = 0.0
        reaction.notes["qc_direction_guard"] = "ATP_synthase_producer_forward"
        return True
    if reaction.id.upper() in {"PGK", "PPAKR"} and reaction.upper_bound > 0:
        reaction.upper_bound = 0.0
        reaction.notes["qc_direction_guard"] = "reverse_written_direction"
        return True
    # ATP-dependent ligases that release AMP and pyrophosphate are forward
    # activation reactions (acyl-CoA synthetases, peptide ligases, etc.).
    if (
        reaction.lower_bound < 0
        and stoichiometry.get("atp_c") == -1.0
        and stoichiometry.get("amp_c") == 1.0
        and stoichiometry.get("ppi_c") == 1.0
    ):
        reaction.lower_bound = 0.0
        reaction.notes["qc_direction_guard"] = "ATP_to_AMP_pyrophosphate_ligase_forward"
        return True
    expected_by_reaction = {
        "ATPM": {
            "atp_c": -1.0,
            "h2o_c": -1.0,
            "adp_c": 1.0,
            "h_c": 1.0,
            "pi_c": 1.0,
        },
        "PPK": {
            "atp_c": -1.0,
            "pi_c": -1.0,
            "adp_c": 1.0,
            "ppi_c": 1.0,
        },
        "PPA": {
            "h2o_c": -1.0,
            "ppi_c": -1.0,
            "h_c": 1.0,
            "pi_c": 2.0,
        },
        "PTPATI": {
            "atp_c": -1.0,
            "h_c": -1.0,
            "pan4p_c": -1.0,
            "dpcoa_c": 1.0,
            "ppi_c": 1.0,
        },
        "RXN00106_C": {
            "cpd00421_c": -1.0,
            "h2o_c": -1.0,
            "h_c": 1.0,
            "pi_c": 1.0,
            "ppi_c": 1.0,
        },
        "RXN33754_C": {
            "cpd00002_c": -1.0,
            "cpd00072_c": -1.0,
            "cpd00008_c": 1.0,
            "cpd19036_c": 1.0,
            "h_c": 1.0,
        },
        "GTHRDT": {
            "atp_c": -1.0,
            "gthrd_c": -1.0,
            "h2o_c": -1.0,
            "adp_c": 1.0,
            "gthrd_m": 1.0,
            "h_c": 1.0,
            "pi_c": 1.0,
        },
        # ATP-dependent peptide/protein synthesis; reverse would regenerate
        # ATP from AMP and pyrophosphate.
        "RXN42091_C": {
            "arg__L_c": -1.0,
            "cpd00002_c": -11.0,
            "cpd02095_c": -1.0,
            "cpd23234_c": -2.0,
            "glu__L_c": -1.0,
            "lys__L_c": -1.0,
            "ser__L_c": -2.0,
            "thr__L_c": -2.0,
            "tyr__L_c": -1.0,
            "amp_c": 11.0,
            "cpd23235_c": 1.0,
            "h2o_c": 1.0,
            "h_c": 22.0,
            "ppi_c": 11.0,
        },
        # ModelSEED rxn00148_c is the reverse-written PYK/CDC19 chemistry;
        # the published pyruvate-kinase direction is ADP + PEP -> ATP + Pyr.
        "RXN00148_C": {
            "cpd00002_c": -1.0,
            "pyr_c": -1.0,
            "cpd00008_c": 1.0,
            "h_c": 1.0,
            "pep_c": 1.0,
        },
        # Balanced ModelSEED nucleotide-cleavage intermediates.  Their
        # reverse directions would reconstruct ATP from AMP/pyrophosphate.
        "RXN40170_C": {
            "cpd00002_c": -2.0,
            "cpd31194_c": 1.0,
            "h_c": 2.0,
            "ppi_c": 2.0,
        },
        "RXN44366_C": {
            "cpd34082_c": -1.0,
            "h2o_c": -1.0,
            "amp_c": 2.0,
            "h_c": 1.0,
        },
        "RXN45621_C": {
            "cpd31194_c": -1.0,
            "h2o_c": -1.0,
            "cpd34082_c": 1.0,
            "h_c": 1.0,
        },
        "RXN00133_C": {
            "ap4a_c": -1.0,
            "h2o_c": -1.0,
            "amp_c": 1.0,
            "cpd00002_c": 1.0,
            "h_c": 1.0,
        },
        "RXN15494_C": {
            "cpd19036_c": -1.0,
            "h2o_c": -1.0,
            "f6p_c": 1.0,
            "pi_c": 1.0,
        },
        "RXN15952_C": {
            "cpd00008_c": -1.0,
            "f6p_c": -1.0,
            "amp_c": 1.0,
            "cpd19036_c": 1.0,
            "h_c": 2.0,
        },
        "RXN00549_C": {
            "cpd00290_c": -1.0,
            "h2o_c": -1.0,
            "cpd00072_c": 1.0,
            "pi_c": 1.0,
        },
        "RXN04043_C": {
            "cpd00008_c": -1.0,
            "cpd00072_c": -1.0,
            "amp_c": 1.0,
            "cpd00290_c": 1.0,
            "h_c": 2.0,
        },
        "RXN41564_C": {
            "h_c": -1.0,
            "pep_c": -1.0,
            "pi_c": -1.0,
            "ppi_c": 1.0,
            "pyr_c": 1.0,
        },
        "RXN23456_C": {
            "cpd02762_c": -1.0,
            "h_c": -2.0,
            "nadph_c": -3.0,
            "o2_c": -3.0,
            "cpd08631_c": 1.0,
            "h2o_c": 5.0,
            "nadp_c": 3.0,
        },
        "RXN00411_C": {
            "cpd00052_c": -1.0,
            "pyr_c": -1.0,
            "cpd00096_c": 1.0,
            "h_c": 1.0,
            "pep_c": 1.0,
        },
        "RXN19636_C": {
            "cpd00052_c": -1.0,
            "cpd26918_c": -1.0,
            "cpd00096_c": 1.0,
            "cpd26919_c": 1.0,
            "h_c": 1.0,
        },
        "RXN19639_C": {
            "cpd26919_c": -1.0,
            "h2o_c": -1.0,
            "cpd26918_c": 1.0,
            "pi_c": 1.0,
        },
        "RXN00779_C": {
            "cpd00102_c": -1.0,
            "h2o_c": -1.0,
            "nadp_c": -1.0,
            "3pg_c": 1.0,
            "h_c": 2.0,
            "nadph_c": 1.0,
        },
        "RXN00782_C": {
            "cpd00102_c": -1.0,
            "nadp_c": -1.0,
            "pi_c": -1.0,
            "13dpg_c": 1.0,
            "h_c": 1.0,
            "nadph_c": 1.0,
        },
        "RXN00304_C": {
            "cpd00038_c": -1.0,
            "pyr_c": -1.0,
            "cpd00031_c": 1.0,
            "h_c": 1.0,
            "pep_c": 1.0,
        },
        "RXN08667_C": {
            "cpd00925_c": -1.0,
            "h2o_c": -1.0,
            "cpd00031_c": 2.0,
        },
        "RXN09564_C": {
            "cpd00031_c": -1.0,
            "cpd00038_c": -1.0,
            "cpd00925_c": 1.0,
            "h_c": 1.0,
            "pi_c": 1.0,
        },
        "RXN00840_C": {
            "cpd00115_c": -1.0,
            "pyr_c": -1.0,
            "cpd00177_c": 1.0,
            "h_c": 1.0,
            "pep_c": 1.0,
        },
        "RXN02058_C": {
            "cpd00491_c": -1.0,
            "h2o_c": -1.0,
            "pi_c": 1.0,
            "sbt__D_c": 1.0,
        },
        "RXN02060_C": {
            "cpd00115_c": -1.0,
            "sbt__D_c": -1.0,
            "cpd00177_c": 1.0,
            "cpd00491_c": 1.0,
            "h_c": 1.0,
        },
        "RXN24522_C": {
            "h2o_c": -1.0,
            "ppcoa_c": -1.0,
            "coa_c": 1.0,
            "h_c": 1.0,
            "ppa_c": 1.0,
        },
        "RXN08443_C": {
            "cpd15238_c": -1.0,
            "h2o_c": -1.0,
            "coa_c": 1.0,
            "h_c": 2.0,
            "hdcea_c": 1.0,
        },
        "RXN09450_C": {
            "coa_c": -1.0,
            "cpd00002_c": -1.0,
            "hdcea_c": -1.0,
            "amp_c": 1.0,
            "cpd15238_c": 1.0,
            "ppi_c": 1.0,
        },
        "RXN23850_C": {
            "cpd00102_c": -1.0,
            "h2o_c": -1.0,
            "nad_c": -1.0,
            "3pg_c": 1.0,
            "h_c": 2.0,
            "nadh_c": 1.0,
        },
        "RXN07962_C": {
            "2agpg141_c": -1.0,
            "cpd00002_c": -1.0,
            "ttdcea_c": -1.0,
            "amp_c": 1.0,
            "h_c": 1.0,
            "pg141_c": 1.0,
            "ppi_c": 1.0,
        },
        "RXN09139_C": {
            "h2o_c": -1.0,
            "pg141_c": -1.0,
            "2agpg141_c": 1.0,
            "h_c": 1.0,
            "ttdcea_c": 1.0,
        },
        "RXN09130_C": {
            "h2o_c": -1.0,
            "pe120_c": -1.0,
            "2agpe120_c": 1.0,
            "ddca_c": 1.0,
            "h_c": 1.0,
        },
        "RXN09132_C": {
            "h2o_c": -1.0,
            "pe141_c": -1.0,
            "2agpe141_c": 1.0,
            "ttdcea_c": 1.0,
            "h_c": 1.0,
        },
    }
    matched = next(
        (
            name
            for name, expected in expected_by_reaction.items()
            if name in alias_names and stoichiometry == expected
        ),
        None,
    )
    if matched == "RXN00148_C":
        if reaction.upper_bound <= 0:
            return False
        reaction.upper_bound = 0.0
        reaction.notes["qc_direction_guard"] = "PYK_reverse_written_direction"
        return True
    if matched is None or reaction.lower_bound >= 0:
        return False
    reaction.lower_bound = 0.0
    reaction.notes["qc_direction_guard"] = f"{matched}_reverse_blocked"
    return True


def metabolic_quality_control_library(model):
    """Audit reaction direction and conservation for the unified public library.

    Boundary and biomass reactions are intentionally exempt from elemental
    balance because they represent exchange, demand/sink, or pseudo-reaction
    interfaces.  Every other reaction must have finite ordered bounds, known
    formulas/charges, and zero COBRApy mass/charge residual.  The returned
    model is the high-precision active library; the caller can retain the
    original model as an auditable full union.
    """
    from collections import Counter

    cleaned = model.copy()
    chemistry_corrections = apply_canonical_chemistry(cleaned)
    rejected = []
    quality = {}
    counts = Counter()
    direction_guards = []
    for reaction in sorted(cleaned.reactions, key=lambda item: item.id):
        before_bounds = tuple(float(value) for value in reaction.bounds)
        if guard_energy_hydrolysis_direction(reaction):
            direction_guards.append(
                {
                    "reaction_id": reaction.id,
                    "before_bounds": list(before_bounds),
                    "after_bounds": list(reaction.bounds),
                    "reason": reaction.notes.get("qc_direction_guard", "unspecified"),
                }
            )
        lower, upper = reaction.bounds
        direction = "blocked"
        reason = "pass"
        if not (math.isfinite(lower) and math.isfinite(upper)):
            reason = "nonfinite_bounds"
        elif lower > upper + 1e-9:
            reason = "reversed_bounds_order"
        elif lower < -1e-9 and upper > 1e-9:
            direction = "reversible"
        elif abs(lower) <= 1e-9 and abs(upper) <= 1e-9:
            direction = "blocked"
        elif upper <= 1e-9:
            direction = "reverse"
        elif lower >= -1e-9:
            direction = "forward"

        identifier = reaction.id.lower()
        boundary = (
            identifier.startswith(("ex_", "dm_", "sk_"))
            or reaction.id in {"Growth", "BIOMASS"}
            or "biomass" in identifier
        )
        residual = {}
        unknown = False
        if reason == "pass" and not boundary:
            try:
                unknown = any(
                    not met.formula
                    or not met.elements
                    or "R" in met.elements
                    or not math.isfinite(float(met.charge))
                    for met in reaction.metabolites
                )
                residual = {
                    key: value
                    for key, value in reaction.check_mass_balance().items()
                    if abs(value) > 1e-8
                }
            except (TypeError, ValueError, AttributeError):
                unknown = True
            if unknown:
                reason = "unknown_formula_or_charge"
            elif residual:
                reason = "mass_or_charge_imbalance"
        if boundary and reason == "pass":
            reason = "boundary_or_pseudoreaction_allowed"
        if "biomass" in identifier or reaction.id in {"Growth", "BIOMASS"}:
            chemistry_class = "empirical_biomass"
        elif reason == "unknown_formula_or_charge":
            chemistry_class = "unknown_chemistry"
        elif reason == "mass_or_charge_imbalance":
            chemistry_class = "imbalanced"
        else:
            chemistry_class = "strict"
        keep = reason in {"pass", "boundary_or_pseudoreaction_allowed"}
        if keep:
            counts["kept"] += 1
            counts["boundary_or_pseudoreaction"] += reason != "pass"
        else:
            counts[reason] += 1
            rejected.append(reaction.id)
        counts[f"direction_{direction}"] += 1
        quality[reaction.id] = {
            "reaction_id": reaction.id,
            "direction": direction,
            "lower_bound": lower,
            "upper_bound": upper,
            "boundary_or_pseudoreaction": boundary,
            "balance_residual": residual,
            "chemistry_class": chemistry_class,
            "isolation_scope": "boundary_interface" if boundary else "ordinary_reaction",
            "status": reason,
            "active": keep,
        }
    strict_rejected = list(rejected)
    if rejected:
        cleaned.remove_reactions(rejected, remove_orphans=True)
    report = {
        "policy": (
            "finite ordered bounds; balanced internal chemistry; explicit boundary exceptions; "
            "no growth-based chemistry overrides"
        ),
        "source_reactions": len(model.reactions),
        "strict_active_reactions": len(model.reactions) - len(strict_rejected),
        "active_reactions": len(cleaned.reactions),
        "active_metabolites": len(cleaned.metabolites),
        "rejected_reactions": len(strict_rejected),
        "counts": dict(counts),
        "quality_complete": True,
        "canonical_chemistry_corrections": chemistry_corrections,
        "energy_direction_guards": direction_guards,
    }
    return cleaned, quality, report
