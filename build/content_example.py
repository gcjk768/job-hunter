"""Example resume content. Copy to content.py (gitignored) and replace with your own.

build_docs.py and nas_agent.py read CONTACT, MASTER and VARIANTS; the shapes below are what they expect.
"""

CONTACT = {
    "name": "Alex Tan",
    "phone": "+65 0000 0000",
    "email": "alex@example.com",
    "linkedin": "linkedin.com/in/example",
    "github": "github.com/example",
    "location": "Singapore",
}

MASTER = {
    "tagline": "Platform & DevOps Engineer  |  Kubernetes · Terraform · CI/CD · Observability",
    "summary": (
        "Platform engineer who owns the path to production for a multi-team estate: reusable CI templates, "
        "Terraform-managed AWS, zero-downtime Kubernetes releases, and security gates in the pipeline."
    ),
    "skills": [
        ("Cloud & Kubernetes", "AWS (EKS, IAM, VPC, S3), Kubernetes, Helm"),
        ("CI/CD & Platform", "GitLab CI, Docker, Terraform, reusable pipeline templates"),
        ("Reliability", "Prometheus, Grafana, SLOs, incident triage, runbooks"),
    ],
    "experience": [
        {
            "title": "DevOps Engineer",
            "company": "Example Corp",
            "dates": "Jan 2023 – Present",
            "subhead": "Platform team for 40 product squads",
            "bullets": [
                "Built the shared CI template library behind 30+ pipelines.",
                "Ran zero-downtime EKS upgrades from the pipeline.",
            ],
        },
    ],
    "projects": [
        {
            "name": "Self-Healing CI Pipeline",
            "context": "personal",
            "text": "LLM failure analysis in CI that opens draft fix merge requests; a human always approves.",
            "stack": "GitLab CI, Ollama, Python",
        },
        {
            "name": "DORA Dashboard",
            "context": "work",
            "text": "Live DORA metrics across every pipeline the platform team owns.",
            "stack": "Node.js, React, GitLab API",
        },
    ],
    "certifications": ["AWS Certified Solutions Architect – Associate"],
    "education": [("B.Sc. Computer Science — Example University", "Relevant coursework: distributed systems.")],
    "extra": "",
}

VARIANTS = [
    {
        "key": "1_Example_Platform_Engineer",
        "company": "Example Bank",
        "role": "Platform Engineer",
        "url": "https://example.com/job",
        "tagline": MASTER["tagline"],
        "summary": MASTER["summary"],
        "projects": ["DORA Dashboard", "Self-Healing CI Pipeline"],
        "letter": ["Why this role, evidence against the posting, the honest gaps, and a close."],
    },
]
