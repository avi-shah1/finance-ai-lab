"""Automatic target discovery: search sources -> candidate companies.

    planner.py    search queries for a buyer/sector/geography (Claude, cheap)
    sources/      web search APIs behind one SearchSource interface (Exa, ...)
    identify.py   group raw results into companies, drop non-companies (no AI)
    triage.py     is each company in scope? what does its page say? (Claude, cheap)
    runner.py     runs the steps above for one search and saves the results

Claude never supplies company names from its own memory: every candidate
comes from a search result, and every fact about it is quoted from the page.
"""
