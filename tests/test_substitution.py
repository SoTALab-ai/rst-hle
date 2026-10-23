from fractions import Fraction

import pytest
from conftest import passing_consistency

from diamond_factory.models import SubstitutionBinding, SubstitutionCase, SubstitutionRelation
from diamond_factory.substitution import Arithmetic, Exact, run_case, verify_substitutions


def physics_case(candidate, kappa="I/2"):
    item = candidate.item.model_copy(update={
        "question": "Use a lossless scattering model with r>0, r!=1 and kappa!=I,-I. Verify S and Bd=0.",
        "answer": "S = I + coefficient*b*transpose(b); d = (kappa,-1).",
        "required_outputs": ["scattering matrix", "dark vector"]})
    bindings = [("r", "2"), ("kappa", kappa), ("omega", "1"), ("b", "vec([1,kappa])"),
                ("T", "1+kappa**2"),
                ("coefficient", "2*I*(1+r)/(T*(1-r))/(omega-I)-2*I*r*(1+r)/(T*(1-r))/(omega-I*r)"),
                ("S", "eye(2)+coefficient*b*transpose(b)")]
    case = SubstitutionCase(name="lossless input power", target_output="scattering matrix",
        question_anchor="lossless scattering model", answer_anchor="S = I + coefficient",
        domain_justification="r=2 is positive and distinct from 1; kappa is neither I nor -I.",
        bindings=[SubstitutionBinding(name=k, expression=v) for k, v in bindings],
        domain_conditions=[SubstitutionRelation(lhs="r",relation="greater",rhs="0"),
                           SubstitutionRelation(lhs="r",relation="not_equal",rhs="1"),
                           SubstitutionRelation(lhs="T",relation="not_equal",rhs="0")],
        lhs="norm2(S*vec([1,0]))", relation="equal", rhs="1")
    return item, case


def test_original_gold_counterexample_computed_exactly(candidate):
    item, case = physics_case(candidate)
    result = run_case(case, item)
    assert result.verdict == "fail" and result.lhs_value == "21/5" and result.rhs_value == "1"
    assert len(result.domain_results) == 3 and all(x.endswith("True") for x in result.domain_results)


def test_real_coupling_control_conserves_all_input_powers(candidate):
    item, case = physics_case(candidate, "1/2")
    case.lhs, case.rhs = "adjoint(S)*S", "eye(2)"
    result = run_case(case, item)
    assert result.verdict == "pass" and result.lhs_value == "[[1, 0], [0, 1]]"


def test_gold_dark_vector_fails_literal_definition(candidate):
    item, case = physics_case(candidate)
    case.name, case.target_output = "literal dark-state definition", "dark vector"
    case.question_anchor, case.answer_anchor = "Bd=0", "d = (kappa,-1)"
    case.bindings.extend([SubstitutionBinding(name="B", expression="mat([[1,1],[kappa,kappa]])"),
                          SubstitutionBinding(name="d", expression="vec([kappa,-1])")])
    case.lhs, case.rhs = "B*d", "vec([0,0])"
    assert run_case(case, item).verdict == "fail"
    case.bindings[-1].expression = "vec([1,-1])"
    # The engine establishes only the equation. Translation from the gold is separately audited.
    assert run_case(case, item).verdict == "pass"


def test_inadmissible_point_is_not_a_counterexample(candidate):
    item, case = physics_case(candidate)
    case.domain_conditions.append(SubstitutionRelation(lhs="r",relation="greater",rhs="3"))
    result = run_case(case, item)
    assert result.verdict == "unsupported" and "violates its declared domain" in result.reason


@pytest.mark.parametrize("expression", ["__import__('os').getcwd()", "I.real", "[1][0]", "1.2", "2**1000000",
                                        "sum([1,2])", "eye(10000)", "inverse(mat([[0]]))", "1/0"])
def test_unsupported_or_unsafe_expression_never_executes_or_passes(candidate, expression):
    item, case = physics_case(candidate)
    case.lhs = expression
    assert run_case(case, item).verdict == "unsupported"


def test_exact_inverse_and_rational_complex_arithmetic():
    evaluator = Arithmetic()
    assert evaluator.expression("(1/3+I/7)*conj(1/3+I/7)") == Exact(Fraction(58,441))
    evaluator.names["A"] = evaluator.expression("mat([[1,I],[2,3]])")
    assert evaluator.expression("inverse(A)*A") == evaluator.expression("eye(2)")


def test_missing_output_coverage_and_unanchored_translation_block_sampling(candidate):
    audit = passing_consistency()
    candidate.item.required_outputs.append("another required output")
    result = verify_substitutions(audit, candidate.item)
    assert result.verdict == "unsupported" and result.uncovered_outputs == ["another required output"]
    audit.substitution_checks[0].answer_anchor = "This is not in the gold"
    assert verify_substitutions(audit, candidate.item).verdict == "unsupported"


def test_noncomputational_proof_is_explicitly_marked_without_arithmetic_coverage(candidate):
    audit = passing_consistency()
    audit.task_kind, audit.substitution_checks = "proof", []
    report = verify_substitutions(audit, candidate.item)
    assert report.verdict == "not_applicable" and report.uncovered_outputs == candidate.item.required_outputs


def test_matrix_dimension_mismatch_and_redefined_constants_cannot_pass(candidate):
    item, case = physics_case(candidate)
    case.lhs, case.rhs = "S*vec([1,2,3])", "vec([1,2])"
    assert run_case(case, item).verdict == "unsupported"
    case.bindings.append(SubstitutionBinding(name="I", expression="1"))
    assert run_case(case, item).verdict == "unsupported"
