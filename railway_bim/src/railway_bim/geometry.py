"""Source geometry in the reconstruction's local coordinate system."""

from __future__ import annotations

import math

import numpy as np


def world(points, alignment):
    """Rotate UVZ to source-local XYZ without applying the global offset."""
    points = np.asarray(points, dtype=float)
    result = np.empty_like(points)
    along, across = alignment["along"], alignment["across"]
    result[..., 0] = points[..., 0] * along[0] + points[..., 1] * across[0]
    result[..., 1] = points[..., 0] * along[1] + points[..., 1] * across[1]
    result[..., 2] = points[..., 2]
    return result


def triangulate_polygon(points):
    """Ear-clip a simple polygon, including the source rail's concave profile."""
    points = np.asarray(points, dtype=float)
    area = np.sum(points[:, 0] * np.roll(points[:, 1], -1)
                  - points[:, 1] * np.roll(points[:, 0], -1))
    remaining = list(range(len(points)))
    if area < 0:
        remaining.reverse()
    triangles = []

    def cross(a, b, c):
        ab, ac = b - a, c - a
        return float(ab[0] * ac[1] - ab[1] * ac[0])

    while len(remaining) > 3:
        found = False
        for pos, middle in enumerate(remaining):
            first, last = remaining[pos - 1], remaining[(pos + 1) % len(remaining)]
            a, b, c = points[[first, middle, last]]
            if cross(a, b, c) <= 1e-12:
                continue
            if any(cross(a, b, points[k]) >= -1e-12
                   and cross(b, c, points[k]) >= -1e-12
                   and cross(c, a, points[k]) >= -1e-12
                   for k in remaining if k not in (first, middle, last)):
                continue
            triangles.append((first, middle, last))
            remaining.pop(pos)
            found = True
            break
        if not found:
            raise ValueError("Source profile cannot be triangulated as a simple polygon")
    triangles.append(tuple(remaining))
    return triangles


def swept_mesh(sections, cap_profile):
    sections = np.asarray(sections, dtype=float)
    section_count, side_count, _ = sections.shape
    faces = []
    caps = triangulate_polygon(cap_profile)
    faces.extend(tuple(reversed(triangle)) for triangle in caps)
    offset = (section_count - 1) * side_count
    faces.extend(tuple(offset + j for j in triangle) for triangle in caps)
    for k in range(section_count - 1):
        for j in range(side_count):
            a = k * side_count + j
            b = k * side_count + (j + 1) % side_count
            c = (k + 1) * side_count + (j + 1) % side_count
            d = (k + 1) * side_count + j
            faces.extend(((a, b, c), (a, c, d)))
    return sections.reshape(-1, 3), faces


def _sample_count(interval, supplied):
    if supplied is not None:
        if isinstance(supplied, bool) or not isinstance(supplied, int) or not 2 <= supplied <= 100_000:
            raise ValueError("Curve sampling needs between two and 100,000 points")
        return supplied
    required = max(2, math.ceil((interval[1] - interval[0]) / .2) + 1)
    if required > 100_000:
        raise ValueError("Segment the corridor before creating curves over 100,000 sampled points")
    return required


def rail_mesh(parameters, alignment, sample_count=None):
    us = np.linspace(*parameters["range_u"], _sample_count(parameters["range_u"], sample_count))
    vs = np.polyval(parameters["v_coefficients"], us)
    zs = np.polyval(parameters["z_coefficients"], us)
    half_head = parameters["head_width_m"] / 2
    # These are the existing model's assumed hidden web and foot dimensions.
    profile = np.asarray([
        [-.075, -.17], [.075, -.17], [.075, -.154], [.008, -.14],
        [.008, -.055], [half_head, -.036], [half_head, -.010], [.026, 0],
        [-.026, 0], [-half_head, -.010], [-half_head, -.036],
        [-.008, -.055], [-.008, -.14], [-.075, -.154],
    ])
    sections = []
    derivative = np.polyder(parameters["v_coefficients"])
    for u, v, z in zip(us, vs, zs):
        tangent = np.asarray([1., np.polyval(derivative, u)])
        normal = np.asarray([-tangent[1], tangent[0]])
        normal /= np.linalg.norm(normal)
        sections.append([[u + normal[0] * x, v + normal[1] * x, z + dz]
                         for x, dz in profile])
    vertices, faces = swept_mesh(sections, profile)
    return world(vertices, alignment), faces


def tube_mesh(path, radius, alignment, sides=8):
    path = np.asarray(path, dtype=float)
    if len(path) < 2:
        raise ValueError("A tube needs at least two source path points")
    circle = np.asarray([[math.cos(2 * math.pi * j / sides),
                          math.sin(2 * math.pi * j / sides)] for j in range(sides)])
    sections = []
    for i, point in enumerate(path):
        tangent = path[min(i + 1, len(path) - 1)] - path[max(i - 1, 0)]
        length = np.linalg.norm(tangent)
        if length < 1e-12:
            raise ValueError("Source path has a zero-length tangent")
        tangent /= length
        normal = np.cross(tangent, [0., 0., 1.])
        if np.linalg.norm(normal) < .01:
            normal = np.cross(tangent, [0., 1., 0.])
        normal /= np.linalg.norm(normal)
        other = np.cross(tangent, normal)
        sections.append([point + radius * (x * normal + y * other) for x, y in circle])
    vertices, faces = swept_mesh(sections, circle)
    return world(vertices, alignment), faces


def trough_mesh(parameters, alignment):
    width, depth = parameters["width_m"], parameters["depth_m"]
    sections = [[[u, v - width / 2, z - depth],
                 [u, v + width / 2, z - depth],
                 [u, v + width / 2, z], [u, v - width / 2, z]]
                for u, v, z in parameters["path_uvz"]]
    profile = [[-width / 2, -depth], [width / 2, -depth],
               [width / 2, 0.], [-width / 2, 0.]]
    vertices, faces = swept_mesh(sections, profile)
    return world(vertices, alignment), faces


def terrain_meshes(terrain_path, spec, alignment, region_definitions):
    """Assign observed cells to the project's ordered terrain selectors."""
    with np.load(terrain_path) as terrain:
        z, valid = terrain["z"], terrain["valid"]
        step = float(terrain["step"])
        u0, v0 = float(terrain["u0"]), float(terrain["v0"])
        nu, nv = z.shape
        u, v = np.meshgrid(u0 + (np.arange(nu) + .5) * step,
                           v0 + (np.arange(nv) + .5) * step, indexing="ij")
        points = np.column_stack((u.ravel(), v.ravel(), z.ravel()))
        regions = {region["id"]: [] for region in region_definitions}
        paths = {trough["id"]: np.asarray(trough["path_uvz"]) for trough in spec.get("troughs", [])}

        def matches(selector, along, lateral):
            kind = selector["kind"]
            if kind == "remainder":
                return True
            if kind == "lateral_range":
                lower, upper = selector.get("minimum_m", -math.inf), selector.get("maximum_m", math.inf)
                return lower <= lateral < upper
            if kind == "between_paths":
                low, high = [paths[identity] for identity in selector["path_ids"]]
                lower = np.interp(along, low[:, 0], low[:, 1])
                upper = np.interp(along, high[:, 0], high[:, 1])
                margin = selector.get("margin_m", 0.)
                return lower - margin < lateral < upper + margin
            if kind == "above_path":
                path = paths[selector["path_id"]]
                return lateral >= np.interp(along, path[:, 0], path[:, 1])
            raise ValueError("Unknown terrain selector: " + str(kind))

        for i in range(nu - 1):
            for j in range(nv - 1):
                if not valid[i:i + 2, j:j + 2].all():
                    continue
                lateral = v[i, j]
                region = next((region["id"] for region in region_definitions
                               if matches(region["selector"], u[i, 0], lateral)), None)
                if region is None:
                    raise ValueError("A valid terrain cell has no declared region")
                k = i * nv + j
                regions[region].extend(((k, k + nv, k + nv + 1), (k, k + nv + 1, k + 1)))
        result = {}
        for name, faces in regions.items():
            if not faces:
                raise ValueError("Terrain region has no complete observed cells: " + name)
            faces = np.asarray(faces, dtype=int)
            used = np.unique(faces)
            mapping = np.full(nu * nv, -1, dtype=int)
            mapping[used] = np.arange(len(used))
            result[name] = (world(points[used], alignment), mapping[faces].tolist())
        return result


def measured_alignment(parameters, alignment, sample_count=None):
    """Average the paired rail paths over their common observed interval."""
    rails = parameters["rails"]
    start = max(r["range_u"][0] for r in rails)
    end = min(r["range_u"][1] for r in rails)
    if end <= start:
        raise ValueError("Paired rails have no shared observed interval")
    us = np.linspace(start, end, _sample_count((start, end), sample_count))
    vs = sum(np.polyval(r["v_coefficients"], us) for r in rails) / len(rails)
    zs = sum(np.polyval(r["z_coefficients"], us) for r in rails) / len(rails)
    return world(np.column_stack((us, vs, zs)), alignment)
