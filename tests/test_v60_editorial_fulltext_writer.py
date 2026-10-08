from __future__ import annotations
from pathlib import Path

from daily_agent.config import load_config
from daily_agent.editorial import approve_publication, draft_report_items, review_draft
from daily_agent.models import MaterialRecord


def test_rule_writer_uses_fulltext_sections_for_quantum_compilation_summary():
    config = load_config(str(Path(__file__).resolve().parents[1]))
    material = MaterialRecord(
        key="arxiv:2607.03275",
        source="arxiv",
        item_type="paper",
        title="Comparing and learning figures of merit for quantum circuit compilation",
        url="https://arxiv.org/abs/2607.03275v1",
        abstract=(
            "Quantum circuits must be compiled for device constraints. The quality is quantified by "
            "figures of merit, but simple metrics trade ease of calculation for execution accuracy."
        ),
        paper_text_excerpt="full text available",
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "section_notes": {
                "introduction": (
                    "Real devices have limited connectivity and varying qubit noise, so compilers need "
                    "figures of merit that predict execution quality instead of only counting depth."
                ),
                "method": (
                    "We propose wPST, a weighted version of probability of successful trials. "
                    "Machine learning models use quantum circuit and hardware data; a two-step process "
                    "first predicts added gates after transpilation and then predicts wPST with coherence times."
                ),
                "results": (
                    "Machine-learning-predicted FoMs outperform commonly used FoMs, increasing correlation "
                    "with true PST or wPST by over 50% in simulations and quantum processor experiments."
                ),
                "limitations": (
                    "No single FoM fully captures compiled circuit performance; relevance varies with "
                    "algorithm, hardware, and experimental objective."
                ),
            },
        },
        categories=["quant-ph"],
        tags=["quantum_ai", "quantum_compilation"],
        score=100,
    )

    drafts = draft_report_items(config, [material], use_llm=False)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)
    fields = drafts[0].draft_fields

    assert reviews[0].verdict == "PASS"
    assert approved
    assert "wPST" in fields["method"]
    assert "machine learning" in fields["method"].lower()
    assert "相对已有工作" in fields["novelty_or_difference"]
    assert "wPST" in fields["novelty_or_difference"]
    assert "50%" in fields["key_result"]
    assert "单一" in fields["limitations"] or "No single FoM" in fields["limitations"]
    assert "酉变换" not in fields["method"]


def test_rule_writer_handles_real_fom_spacing_and_fragmented_limitations():
    config = load_config(str(Path(__file__).resolve().parents[1]))
    material = MaterialRecord(
        key="arxiv:2607.03275",
        source="arxiv",
        item_type="paper",
        title="Comparing and learning figures of merit for quantum circuit compilation",
        url="https://arxiv.org/abs/2607.03275v1",
        abstract=(
            "Quantum circuits must be compiled for device constraints. The quality is quantified by "
            "figures of merit, but simple metrics trade ease of calculation for execution accuracy."
        ),
        paper_text_excerpt="full text available",
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "section_notes": {
                "method": (
                    "We propose wPST, a weighted version of probability of successful trials. "
                    "Machine learning models use quantum circuit and hardware data."
                ),
                "results": (
                    "In numerical simulations and experiments on quantum processors, we find that our "
                    "machine learning-predicted FoMs outperform commonly used FoMs, increasing the correlation "
                    "with the true PST or wPST by over 50 %."
                ),
                "limitations": (
                    "limitation, more hardware-aware FoMs have been proposed. "
                    "No single FoM fully captures compiled circuit performance across algorithms, hardware, "
                    "and experimental objectives."
                ),
            },
        },
        categories=["quant-ph"],
        tags=["quantum_ai", "quantum_compilation"],
        score=100,
    )

    drafts = draft_report_items(config, [material], use_llm=False)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)
    fields = drafts[0].draft_fields

    assert reviews[0].verdict == "PASS"
    assert approved
    assert "over 50%" in fields["key_result"]
    assert "不存在单一 FoM" in fields["limitations"]
    assert not fields["limitations"].lower().startswith("limitation,")


def test_rule_writer_prefers_numeric_result_sentence_over_abstract_preamble():
    config = load_config(str(Path(__file__).resolve().parents[1]))
    material = MaterialRecord(
        key="arxiv:2607.02865",
        source="arxiv",
        item_type="paper",
        title="DREAMSTEER: Latent World Models Can Steer VLA Policies During Deployment Without Any Finetuning",
        url="https://arxiv.org/abs/2607.02865v1",
        abstract="Pretrained VLA policies show promising zero-shot generalization but fail under distribution shift.",
        paper_text_excerpt="full text available",
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "section_notes": {
                "abstract": (
                    "Abstract: Pretrained vision-language-action policies show promising zero-shot generalization, "
                    "but often fail under deployment-time distribution shift. "
                    "Across four real-world manipulation benchmarks with unseen objects, DREAMSTEER improves task "
                    "success rate from 23.75% to 66.25% and instruction-following accuracy from 38.75% to 56.25%."
                ),
                "method": (
                    "DREAMSTEER samples candidate action chunks from a VLA policy and predefined motion primitives, "
                    "imagines their outcomes using an action-conditioned latent world model, and ranks the imagined "
                    "trajectories with a language-conditioned value model."
                ),
                "results": "resulting trajectories before execution.",
                "limitations": (
                    "Limitations 5.1 Why DREAMSTEER Works Trajectory Ranking Over One-pass Generation. "
                    "DREAMSTEER still depends on the world model and value model generalizing to unseen objects and environments."
                ),
            },
        },
        categories=["cs.RO"],
        tags=["world_model", "vla", "agent"],
        score=100,
    )

    drafts = draft_report_items(config, [material], use_llm=False)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)
    fields = drafts[0].draft_fields

    assert reviews[0].verdict == "PASS"
    assert approved
    assert "23.75%" in fields["key_result"]
    assert "66.25%" in fields["key_result"]
    assert "show promising" not in fields["key_result"]
    assert "依赖 world model 和 value model" in fields["limitations"]


def test_rule_writer_summarizes_quantum_compiler_pass_tuning_without_copying_result_sentence():
    config = load_config(str(Path(__file__).resolve().parents[1]))
    material = MaterialRecord(
        key="arxiv:2607.04586",
        source="arxiv",
        item_type="paper",
        title="QuTuner: Feature- and Learning-Guided Optimization Pass Tuning for Quantum Compilers",
        url="https://arxiv.org/abs/2607.04586v1",
        abstract="Quantum compilers need better optimization pass tuning.",
        paper_text_excerpt="full text available",
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "section_notes": {
                "abstract": (
                    "Prior quantum compiler tuning approaches search only a small portion of the optimization-pass "
                    "space and rely on static features."
                ),
                "method": (
                    "We present QuTuner, a feature-guided quantum compiler pass tuning framework. QuTuner builds "
                    "a dataset by running Bayesian Optimization on 8,111 quantum circuits, combines static circuit "
                    "features with optimization-aware pass embeddings, and retrieves and ranks candidate pass sequences."
                ),
                "results": (
                    "On Qiskit, QuTuner improves the evaluation-metric reduction by up to 84.85% over the strongest "
                    "baseline while reducing tuning time by 73.59%. On PyTKET, it improves metric reduction by up to "
                    "18.68% with a 64.49% reduction in tuning time."
                ),
                "limitations": (
                    "Although optimization passes can interact with each other, profiling pass combinations would "
                    "substantially increase both profiling cost and embedding dimension."
                ),
            },
        },
        categories=["quant-ph"],
        tags=["quantum_circuit", "quantum_compilation"],
        score=100,
    )

    drafts = draft_report_items(config, [material], use_llm=False)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)
    fields = drafts[0].draft_fields

    assert reviews[0].verdict == "PASS"
    assert approved
    assert "8,111" in fields["method"]
    assert "84.85%" in fields["key_result"]
    assert "73.59%" in fields["key_result"]
    assert not fields["key_result"].startswith("On Qiskit")
    assert "pass 组合" in fields["limitations"]


def test_rule_writer_does_not_misclassify_vla_world_model_as_qec():
    config = load_config(str(Path(__file__).resolve().parents[1]))
    material = MaterialRecord(
        key="arxiv:2607.02865",
        source="arxiv",
        item_type="paper",
        title="DREAMSTEER: Latent World Models Can Steer VLA Policies During Deployment Without Any Finetuning",
        url="https://arxiv.org/abs/2607.02865v1",
        abstract="Pretrained VLA policies fail under deployment-time distribution shift.",
        paper_text_excerpt="full text available",
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "section_notes": {
                "abstract": (
                    "Pretrained vision-language-action policies often fail under deployment-time distribution shift. "
                    "DREAMSTEER samples candidate action chunks, imagines outcomes with an action-conditioned latent "
                    "world model, and ranks trajectories with a language-conditioned value model."
                ),
                "method": (
                    "A frozen VLA policy proposes candidate action chunks; predefined Cartesian primitives are added. "
                    "The latent world model predicts future observations and the value model ranks trajectories before execution."
                ),
                "results": (
                    "Across four real-world manipulation benchmarks, DREAMSTEER improves task success rate from "
                    "23.75% to 66.25% and instruction-following accuracy from 38.75% to 56.25%."
                ),
                "limitations": (
                    "The framework still depends on the latent world model and value model generalizing to unseen objects and environments."
                ),
            },
        },
        categories=["cs.RO"],
        tags=["world_model", "vla", "agent"],
        score=100,
    )

    drafts = draft_report_items(config, [material], use_llm=False)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)
    fields = drafts[0].draft_fields

    assert reviews[0].verdict == "PASS"
    assert approved
    assert "QEC" not in fields["problem"]
    assert "latent world model" in fields["method"].lower()
    assert "value model" in fields["why_it_works"].lower()
    assert "23.75%" in fields["key_result"]


def test_rule_writer_paraphrases_qnn_negative_result():
    config = load_config(str(Path(__file__).resolve().parents[1]))
    material = MaterialRecord(
        key="arxiv:2607.04915",
        source="arxiv",
        item_type="paper",
        title="How Hard Is Quantum Advantage? A Cloud Microphysics Stress Test for Variational Quantum Models",
        url="https://arxiv.org/abs/2607.04915v1",
        abstract="Quantum machine learning lacks strong evidence of actual improvements and scalability.",
        paper_text_excerpt="full text available",
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "section_notes": {
                "abstract": (
                    "We employ a hybrid quantum neural network on a cloud microphysics dataset. "
                    "To reach optimal performance, QNNs use a rich trainable frequency spectrum, "
                    "expressivity-enhancing classical postprocessing, and extensive hyperparameter optimization. "
                    "At the same time, QNNs are outperformed by classical baselines in the form of fully-connected neural networks."
                ),
                "results": (
                    "We show that QNNs, despite recent improvements and dedicated optimization, perform similarly to unoptimized neural networks. "
                    "However, optimized neural networks strongly outperform the QNN architecture."
                ),
                "limitations": "Limitations of QNNs in cloud microphysics: worse R2 scores across the board and unclear scalability.",
            },
        },
        categories=["quant-ph"],
        tags=["qnn", "quantum_ai"],
        score=100,
    )

    drafts = draft_report_items(config, [material], use_llm=False)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)
    fields = drafts[0].draft_fields

    assert reviews[0].verdict == "PASS"
    assert approved
    assert "frequency spectrum" in fields["method"]
    assert "FCNN" in fields["key_result"]
    assert "QEC" not in fields["possible_use_or_impact"]


def test_rule_writer_uses_opine_world_evaluation_not_formula_definition():
    config = load_config(str(Path(__file__).resolve().parents[1]))
    material = MaterialRecord(
        key="arxiv:2607.01531",
        source="arxiv",
        item_type="paper",
        title="OPINE-World: Programmatic World Modeling with Ontology-error-Prioritized Interactive Exploration",
        url="https://arxiv.org/abs/2607.01531v1",
        abstract="OPINE-World learns an object-centric programmatic world model online from interaction.",
        paper_text_excerpt="full text available",
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "section_notes": {
                "abstract": (
                    "OPINE-World couples two cooperating agents in a loop of hypothesis and test: one acts in the environment "
                    "and one synthesizes the model in code with replay verification and model-based planning."
                ),
                "method": (
                    "It discovers the object ontology from interaction and uses counterexample-guided synthesis to keep the program model consistent."
                ),
                "results": (
                    "OPINE-World solves 20 of 25 games and 160 of 183 levels without per-game training, exceeding a strong single-agent coding agent. "
                    "A game is a tuple (C, A, T, R, s0) with object classes C and a deterministic transition T."
                ),
                "limitations": (
                    "Hidden-state games are out of scope; inferred perception and bounded planner scale remain limitations."
                ),
            },
        },
        categories=["cs.AI"],
        tags=["world_model", "llm", "agent"],
        score=100,
    )

    drafts = draft_report_items(config, [material], use_llm=False)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)
    fields = drafts[0].draft_fields

    assert reviews[0].verdict == "PASS"
    assert approved
    assert "two cooperating agents" in fields["method"]
    assert "20/25" in fields["key_result"]
    assert "tuple" not in fields["key_result"]


def test_rule_writer_does_not_apply_qnn_template_to_hamqasbench():
    config = load_config(str(Path(__file__).resolve().parents[1]))
    material = MaterialRecord(
        key="arxiv:2607.04845",
        source="arxiv",
        item_type="paper",
        title="HamQASBench: A Hamiltonian-Informed Diagnostic Benchmark for Evaluating Quantum Architecture Search",
        url="https://arxiv.org/abs/2607.04845v1",
        abstract="Quantum Architecture Search needs benchmarks that expose structural failures beyond energy accuracy.",
        paper_text_excerpt="full text available",
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "section_notes": {
                "abstract": (
                    "We introduce HAMQASBENCH, a Hamiltonian-informed diagnostic benchmark organizing 11 molecules "
                    "into five structural tiers via fingerprints derived from Pauli operator basis, computational basis representation, "
                    "and ground-state entanglement."
                ),
                "method": (
                    "A post-hoc critical-structure extraction procedure identifies minimal circuits consistent with each tier; "
                    "benchmarking five QAS methods reveals over-parameterization, eigenstate commitment, topology-induced routing failure, "
                    "and circuit search space growth."
                ),
                "conclusion": (
                    "Across five tiers spanning eleven molecules and up to 14 qubits, energy accuracy is an unreliable proxy for structural correctness."
                ),
                "limitations": (
                    "Extending the analysis to other paradigms remains future work; ground-state degeneracy introduces additional ambiguity."
                ),
            },
        },
        categories=["quant-ph"],
        tags=["quantum_ai", "quantum_circuit"],
        score=100,
    )

    drafts = draft_report_items(config, [material], use_llm=False)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)
    fields = drafts[0].draft_fields

    assert reviews[0].verdict == "PASS"
    assert approved
    assert "hybrid QNN" not in fields["method"]
    assert "Hamiltonian structural fingerprints" in fields["method"]
    assert "14 qubits" in fields["key_result"]
