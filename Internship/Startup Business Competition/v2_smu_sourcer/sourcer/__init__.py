"""
smu_sourcer - automatically source startup founders (URL, name, email, industry).

A clean, pluggable pipeline that redesigns smu_crawler around one goal and one record:

    sources  ->  enrich (Claude on Bedrock + deterministic)  ->  email-find  ->  export

Sources (accelerator portfolios, university competition winners, web/news search, and
the Tracxn paid database) each discover startups; enrichment reads each startup's own
site for the founder's name, role, and email plus the industry; email-find fills any
missing addresses by pattern + MX verification. See README.md.
"""
__version__ = "2.0.0"
