"""SkyPoints loyalty programme data ingestion.

Two daily source feeds are landed, validated, conformed and routed into
per-country target tables:

* a pipe-delimited flat file of member profile data, and
* a semi-structured JSON feed of partner redemption transactions.
"""

__version__ = "0.1.0"
