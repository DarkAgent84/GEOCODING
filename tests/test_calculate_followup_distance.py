import math
import pytest
from scripts.calculate_followup_distance import haversine_distance


def test_haversine_distance_same_point():
    # Same point should return 0.0
    dist = haversine_distance(19.0760, 72.8777, 19.0760, 72.8777, unit="km")
    assert dist == 0.0
    dist_m = haversine_distance(19.0760, 72.8777, 19.0760, 72.8777, unit="m")
    assert dist_m == 0.0


def test_haversine_distance_known_cities():
    # Mumbai (18.9220, 72.8347) to Pune (18.5204, 73.8567) ~ 118-120 km
    dist_km = haversine_distance(18.9220, 72.8347, 18.5204, 73.8567, unit="km")
    assert 115.0 < dist_km < 125.0

    dist_m = haversine_distance(18.9220, 72.8347, 18.5204, 73.8567, unit="m")
    assert 115000.0 < dist_m < 125000.0


def test_haversine_distance_none_handling():
    assert haversine_distance(None, 72.0, 19.0, 73.0) is None
    assert haversine_distance(19.0, None, 19.0, 73.0) is None
    assert haversine_distance(19.0, 72.0, None, 73.0) is None
    assert haversine_distance(19.0, 72.0, 19.0, None) is None
