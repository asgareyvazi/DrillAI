"""Independently constructed 3-D line-segment references, not collision-risk validation."""
import pytest

from drillai.core.errors import EngineInputInvalid


def path(points, revision="A", frame="grid-A", datum="MSL"):
    return {"revision_reference": revision, "coordinate_reference": frame, "vertical_datum": datum,
            "source_reference": "synthetic-only", "depth_positive": "down", "points": [
                {"md_si": md, "north_si": north, "east_si": east, "tvd_si": tvd}
                for md, north, east, tvd in points]}


@pytest.mark.parametrize("ref,offset,distance,ref_md,offset_md", [
    ([(0, 0, 0, 0), (10, 0, 0, 10)], [(0, 3, 4, 0), (10, 3, 4, 10)], 5, 0, 0),
    ([(0, -5, 0, 0), (10, 5, 0, 0)], [(0, 0, -5, 0), (10, 0, 5, 0)], 0, 5, 5),
    ([(0, 0, 0, 0), (10, 10, 0, 0)], [(0, 5, -5, 3), (10, 5, 5, 3)], 3, 5, 5),
    ([(0, 0, 0, 0), (10, 0, 0, 10)], [(0, 0, 0, 20), (10, 0, 0, 30)], 10, 10, 0),
])
def test_closest_3d_segments_against_synthetic_geometries(engines, ref, offset, distance, ref_md, offset_md):
    spec, result = engines.execute("trajectory.geometric_proximity", {"reference": path(ref), "offset": path(offset, revision="B")})
    assert spec.version == "1.0.0"
    assert result.outputs.classification == "GEOMETRIC_PROXIMITY_ONLY"
    assert result.outputs.distance_si == pytest.approx(distance, abs=1e-9)
    assert result.outputs.reference_md_si == pytest.approx(ref_md, abs=1e-9)
    assert result.outputs.offset_md_si == pytest.approx(offset_md, abs=1e-9)
    assert any("uncertainty" in warning for warning in result.warnings)


def test_proximity_rejects_mismatched_frame_datum_and_unordered_depths(engines):
    points = [(0, 0, 0, 0), (10, 0, 0, 10)]
    for other in (path(points, frame="grid-B"), path(points, datum="RKB"), path(list(reversed(points))), path(points, frame="")):
        with pytest.raises(EngineInputInvalid):
            engines.execute("trajectory.geometric_proximity", {"reference": path(points), "offset": other})


def test_nearly_parallel_segments_fail_closed(engines):
    from drillai.core.errors import EngineConvergenceError

    ref = path([(0, 0, 0, 0), (100, 100, 0, 0)])
    offset = path([(0, 0, -0.0000005, 0), (100, 100, 0.0000005, 0)])
    with pytest.raises(EngineConvergenceError):
        engines.execute("trajectory.geometric_proximity", {"reference": ref, "offset": offset})
