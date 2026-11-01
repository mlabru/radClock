from math import cos, radians

import pytest

from radClock.core.projection import R_EARTH_KM, AzimuthalEquidistant, dist_bearing

DEG_KM = R_EARTH_KM * radians(1)


def test_origin_is_zero():
    p = AzimuthalEquidistant(-23.21, -45.87)
    assert p.forward(-23.21, -45.87) == (0.0, 0.0)


def test_one_degree_north():
    p = AzimuthalEquidistant(-23.21, -45.87)
    e, n = p.forward(-22.21, -45.87)
    assert e == pytest.approx(0.0, abs=1e-9)
    assert n == pytest.approx(DEG_KM, rel=1e-9)
    assert dist_bearing(e, n)[1] == pytest.approx(0.0, abs=1e-6)


def test_distance_from_center_is_great_circle():
    # a propriedade que justifica a projeção: distância ao centro é exata
    p = AzimuthalEquidistant(0.0, 0.0)
    e, n = p.forward(0.0, 3.0)
    assert dist_bearing(e, n) == pytest.approx((3 * DEG_KM, 90.0))


def test_antimeridian_is_continuous():
    # o firmware ESP32 jogaria este ponto a ~40.000 km
    p = AzimuthalEquidistant(-17.0, 179.5)
    e, n = p.forward(-17.0, -179.5)
    assert e == pytest.approx(DEG_KM * cos(radians(17)), rel=1e-3)
    assert abs(n) < 2.0
