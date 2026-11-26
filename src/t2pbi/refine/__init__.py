"""Refinement: an agent fixes what the deterministic pass got wrong, one page at a time.

``worklist`` builds what's wrong (no oracle needed); ``workspace`` makes every fix a reviewable,
reversible git commit. The agent, its tools and the review gate arrive in later increments; the
graph runs all of it only with ``t2pbi migrate --refine``.
"""
