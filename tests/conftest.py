"""Tests run on the fixed seed catalog (data/restaurants.json), not the live database."""
import os

os.environ["FOODAGENT_CATALOG"] = "fixture"
