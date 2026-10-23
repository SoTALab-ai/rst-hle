"""对不可信的反代证书执行有界精确算术。

不执行eval、模型代码、导入或文件/网络操作。公式翻译与域条件仍需独立核对；
有限点通过不能证明全域成立。
"""
import ast
from dataclasses import dataclass
from fractions import Fraction

from .models import SubstitutionReport, SubstitutionResult


@dataclass(frozen=True)
class Exact:
    real: Fraction = Fraction(0)
    imag: Fraction = Fraction(0)

    def __add__(self, other):
        if not isinstance(other, Exact):
            return NotImplemented
        return Exact(self.real + other.real, self.imag + other.imag)

    def __neg__(self):
        return Exact(-self.real, -self.imag)

    def __sub__(self, other):
        return self + (-other)

    def __mul__(self, other):
        if not isinstance(other, Exact):
            return NotImplemented
        return Exact(self.real*other.real - self.imag*other.imag, self.real*other.imag + self.imag*other.real)

    def __truediv__(self, other):
        denominator = other.real**2 + other.imag**2
        if not denominator:
            raise ValueError("Division by zero at the proposed test point")
        return self * Exact(other.real/denominator, -other.imag/denominator)

    def __pow__(self, exponent):
        if exponent < 0:
            return (ONE/self)**(-exponent)
        result = ONE
        for _ in range(exponent):
            result = result*self
        return result

    def conjugate(self):
        return Exact(self.real, -self.imag)

    def __str__(self):
        return str(self.real) if not self.imag else f"({self.real})+({self.imag})*I"


ZERO, ONE = Exact(), Exact(Fraction(1))


@dataclass(frozen=True)
class Matrix:
    rows: tuple[tuple[Exact, ...], ...]

    def __post_init__(self):
        if (not 1 <= len(self.rows) <= 8 or not 1 <= len(self.rows[0]) <= 8
                or len({len(row) for row in self.rows}) != 1
                or any(not isinstance(x, Exact) for row in self.rows for x in row)):
            raise ValueError("Expected a rectangular exact matrix of size at most 8 by 8")

    @property
    def shape(self):
        return len(self.rows), len(self.rows[0])

    def __add__(self, other):
        if not isinstance(other, Matrix) or self.shape != other.shape:
            raise ValueError("Matrix addition requires identical spaces/dimensions")
        return Matrix(tuple(tuple(a+b for a, b in zip(x, y, strict=True))
                            for x, y in zip(self.rows, other.rows, strict=True)))

    def __neg__(self):
        return self * (-ONE)

    def __sub__(self, other):
        return self + (-other)

    def __mul__(self, other):
        if isinstance(other, Exact):
            return Matrix(tuple(tuple(x*other for x in row) for row in self.rows))
        if not isinstance(other, Matrix) or self.shape[1] != other.shape[0]:
            raise ValueError("Matrix multiplication has incompatible spaces/dimensions")
        return Matrix(tuple(tuple(sum((self.rows[i][k]*other.rows[k][j] for k in range(self.shape[1])), ZERO)
                                  for j in range(other.shape[1])) for i in range(self.shape[0])))

    def __rmul__(self, other):
        return self * other

    def __truediv__(self, other):
        return self * (ONE/other)

    def transpose(self):
        return Matrix(tuple(zip(*self.rows, strict=True)))

    def conjugate(self):
        return Matrix(tuple(tuple(x.conjugate() for x in row) for row in self.rows))

    def __str__(self):
        return "[" + ", ".join("[" + ", ".join(map(str, row)) + "]" for row in self.rows) + "]"


def integer(value):
    if not isinstance(value, Exact) or value.imag or value.real.denominator != 1:
        raise ValueError("Expected an exact integer")
    return value.real.numerator


def identity(value):
    n = integer(value)
    if not 1 <= n <= 8:
        raise ValueError("Identity matrix size exceeds supported range")
    return Matrix(tuple(tuple(ONE if i == j else ZERO for j in range(n)) for i in range(n)))


def inverse(value):
    if not isinstance(value, Matrix) or value.shape[0] != value.shape[1]:
        raise ValueError("Inverse requires a square matrix")
    n = value.shape[0]
    rows = [list(a+b) for a, b in zip(value.rows, identity(Exact(Fraction(n))).rows, strict=True)]
    for column in range(n):
        pivot = next((i for i in range(column, n) if rows[i][column] != ZERO), None)
        if pivot is None:
            raise ValueError("Matrix is singular at the proposed test point")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        divisor = rows[column][column]
        rows[column] = [x/divisor for x in rows[column]]
        for i in range(n):
            if i != column:
                factor = rows[i][column]
                rows[i] = [a-factor*b for a, b in zip(rows[i], rows[column], strict=True)]
    return Matrix(tuple(tuple(row[n:]) for row in rows))


def norm2(value):
    values = (x for row in value.rows for x in row) if isinstance(value, Matrix) else [value]
    return sum((x.conjugate()*x for x in values), ZERO)


class Arithmetic:
    def __init__(self):
        self.names = {"I": Exact(Fraction(0), Fraction(1))}

    def expression(self, text):
        if len(text) > 3000:
            raise ValueError("Expression is too long")
        tree = ast.parse(text, mode="eval")
        if len(list(ast.walk(tree))) > 240:
            raise ValueError("Expression is too large")
        return self.node(tree.body)

    def node(self, node):
        result = self._node(node)
        values = [x for row in result.rows for x in row] if isinstance(result, Matrix) else [result]
        for value in values:
            if isinstance(value, Exact) and any(max(abs(x.numerator).bit_length(), x.denominator.bit_length()) > 4096
                                              for x in (value.real, value.imag)):
                raise ValueError("Exact arithmetic size limit exceeded")
        return result

    def _node(self, node):
        if isinstance(node, ast.Constant) and type(node.value) is int and abs(node.value).bit_length() <= 128:
            return Exact(Fraction(node.value))
        if isinstance(node, ast.Name) and node.id in self.names:
            return self.names[node.id]
        if isinstance(node, ast.List):
            return [self.node(x) for x in node.elts]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self.node(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            lhs, rhs = self.node(node.left), self.node(node.right)
            if isinstance(node.op, ast.Add):
                return lhs+rhs
            if isinstance(node.op, ast.Sub):
                return lhs-rhs
            if isinstance(node.op, ast.Mult):
                return lhs*rhs
            if isinstance(node.op, ast.Div):
                return lhs/rhs
            if isinstance(node.op, ast.Pow) and isinstance(lhs, Exact) and -8 <= (n := integer(rhs)) <= 8:
                return lhs**n
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords and len(node.args) == 1:
            functions = {"mat": lambda x: Matrix(tuple(tuple(row) for row in x)),
                         "vec": lambda x: Matrix(tuple((a,) for a in x)), "eye": identity,
                         "transpose": lambda x: x.transpose(), "conj": lambda x: x.conjugate(),
                         "adjoint": lambda x: x.conjugate().transpose(), "inverse": inverse, "norm2": norm2}
            if node.func.id in functions:
                return functions[node.func.id](self.node(node.args[0]))
        raise ValueError("Unsupported expression: only bounded rational/complex/matrix arithmetic is allowed")


def compare(a, relation, b):
    if not isinstance(a, (Exact, Matrix)) or not isinstance(b, (Exact, Matrix)):
        raise TypeError("Comparisons require exact scalars or matrices")
    if relation in ("equal", "not_equal"):
        if type(a) is not type(b) or (isinstance(a, Matrix) and a.shape != b.shape):
            raise ValueError("Comparison requires the same scalar or matrix space")
        return (a == b) if relation == "equal" else (a != b)
    if not isinstance(a, Exact) or not isinstance(b, Exact) or a.imag or b.imag:
        raise ValueError("Ordered comparison requires real scalars")
    return a.real > b.real if relation == "greater" else a.real >= b.real


def run_case(case, item):
    lhs, rhs = "", ""
    domain_results = []
    try:
        if case.question_anchor not in item.question or case.answer_anchor not in item.answer:
            raise ValueError("Check is not anchored to the literal question and gold answer")
        if case.target_output not in item.required_outputs:
            raise ValueError("Check targets an output not requested by the item")
        arithmetic = Arithmetic()
        for binding in case.bindings:
            if binding.name in arithmetic.names:
                raise ValueError("A binding or the constant I was redefined")
            value = arithmetic.expression(binding.expression)
            if not isinstance(value, (Exact, Matrix)):
                raise TypeError("Bindings must be exact scalars or matrices")
            arithmetic.names[binding.name] = value
        for condition in case.domain_conditions:
            a, b = arithmetic.expression(condition.lhs), arithmetic.expression(condition.rhs)
            passed = compare(a, condition.relation, b)
            domain_results.append(f"{a} {condition.relation} {b}: {passed}")
            if not passed:
                return SubstitutionResult(name=case.name, target_output=case.target_output, verdict="unsupported",
                    lhs_value="", rhs_value="", domain_results=domain_results,
                    reason="The chosen test point violates its declared domain; it cannot refute the gold.")
        a, b = arithmetic.expression(case.lhs), arithmetic.expression(case.rhs)
        if not isinstance(a, (Exact, Matrix)) or not isinstance(b, (Exact, Matrix)):
            raise TypeError("A comparison must contain exact scalars or matrices")
        lhs, rhs = str(a), str(b)
        passed = compare(a, case.relation, b)
        return SubstitutionResult(name=case.name, target_output=case.target_output,
            verdict="pass" if passed else "fail", lhs_value=lhs, rhs_value=rhs, domain_results=domain_results,
            reason="Exact substitution satisfies the asserted relation." if passed
                   else "Exact substitution contradicts the asserted relation.")
    except (ValueError, TypeError, AttributeError, SyntaxError, RecursionError, OverflowError, ZeroDivisionError) as error:
        return SubstitutionResult(name=case.name, target_output=case.target_output, verdict="unsupported",
            lhs_value=lhs, rhs_value=rhs, domain_results=domain_results,
            reason="Check not verified: " + str(error)[:250])


def verify_substitutions(audit, item):
    results = [run_case(case, item) for case in audit.substitution_checks]
    covered = {r.target_output for r in results if r.verdict == "pass"}
    uncovered = [name for name in item.required_outputs if name not in covered]
    if any(r.verdict == "fail" for r in results):
        verdict, reason = "fail", "An exact counterexample refutes a required gold identity."
    elif any(r.verdict == "unsupported" for r in results):
        verdict, reason = "unsupported", "At least one proposed executable check could not be verified."
    elif audit.task_kind == "computational" and uncovered:
        verdict, reason = "unsupported", "Computational outputs lack executable substitution coverage."
    elif not results:
        verdict, reason = "not_applicable", "No executable arithmetic certificate supplied for this noncomputational task."
    else:
        verdict, reason = "pass", "All supplied substitutions pass; finite checks are not a proof of a universal claim."
    return SubstitutionReport(verdict=verdict, results=results, uncovered_outputs=uncovered, reason=reason)
