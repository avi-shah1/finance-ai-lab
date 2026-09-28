"""Level 2 screening: lightweight evidence for promising candidates, then a
Claude assessment of each against the buyer.

    evidence.py   free sources (website, registers) + cheap AI extraction
    news.py       recent developments from one news search per company
    buyer.py      the buyer's own profile, gathered once and reused
    assess.py     buyer-fit ratings that must cite evidence IDs
    runner.py     screens a search's top candidates in parallel

Expensive work (downloading and AI-reading filed accounts) is not done
here: that is Deep Research, run only when requested.
"""
