from answer.verification import ExtractiveGrounder

G = ExtractiveGrounder()

CASES = [
    (
        "denied_quotation",
        "The project was cancelled.",
        'Alice denied the claim: "The project was cancelled."',
    ),
    ("denied_quotation_colon", "Revenue was zero.", "Bob denied the assertion: revenue was zero."),
    ("attributed_speech", "The treaty was signed.", "Carol said the treaty was signed."),
    ("attributed_fronted", "The treaty was signed.", "According to Carol, the treaty was signed."),
    ("attributed_reported", "The treaty was signed.", "The treaty was signed, said Carol."),
    ("conditional", "The budget is 900.", "If the audit passes, the budget is 900."),
    ("qualified", "The budget is 900.", "About 900 people attended."),
    ("qualified_ro", "Bugetul este de 900.", "Bugetul este de aproximativ 900."),
]

def test_denied_and_attributed_not_verified():
    for name, claim, chunk in CASES:
        assert G.score(claim, chunk) < G.threshold, f"{name} incorrectly grounded"

def test_plain_assertion_grounded():
    assert G.score("The treaty was signed.", "The treaty was signed in 1994.") >= G.threshold
