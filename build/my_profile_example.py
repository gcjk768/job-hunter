"""Copy to my_profile.py (gitignored) and set your own values."""

SALARY_FLOOR = 7000  # SGD/month base; a posted band is judged by its bottom

# Your current employer and anyone you won't work for, as one regex alternation.
EXCLUDED_EMPLOYERS = r"\bEXAMPLE CURRENT EMPLOYER\b|\bACME\b"
EXCLUDED_EMPLOYERS_TEXT = "your current employer"
EXCLUDED_SAMPLE = "Acme Pte Ltd"  # selftest: must match EXCLUDED_EMPLOYERS
