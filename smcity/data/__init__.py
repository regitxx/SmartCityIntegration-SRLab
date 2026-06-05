"""Local data mirrors for the smcity agent.

`poi_store` is a SQLite + R*Tree mirror of the OpenStreetMap POI categories the
agent searches, so `find_poi` can answer from a local spatial index instead of
hitting public `overpass-api.de` on every turn (latency + 504 flakiness).
`poi_refresh` rebuilds that mirror nightly from the SAME Overpass query the live
tool uses, so the two paths cannot drift.
"""
