"""Dataset parsing and the geometry shown in figure 1 of the paper."""

from __future__ import annotations

import ast
import csv
from dataclasses import dataclass
from pathlib import Path

Point = tuple[float, float]
DEFAULT_DATA = (
    Path(__file__).resolve().parents[1] / "instances" / "PRP-UW-instances.txt"
)


@dataclass(frozen=True)
class Layout:
    n: int
    m: int
    width: float = 1.3
    gap: float = 0.05
    clearance: float = 0.65

    def __post_init__(self) -> None:
        if (
            self.n < 1
            or self.m < 1
            or min(self.width, self.clearance) <= 0
            or self.gap < 0
        ):
            raise ValueError("Invalid layout dimensions")

    @property
    def pitch(self) -> float:
        return self.width + self.gap

    @property
    def count(self) -> int:
        return 4 * self.n + 2 * self.m

    @property
    def corridor_half_height(self) -> float:
        return (self.m * self.width + (self.m + 1) * self.gap) / 2

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """xmin, xmax, ymin, ymax for the depot centre."""
        shelf_end = self.n * self.width + (self.n - 1) * self.gap
        vertical_centre = shelf_end - self.width / 2
        return (
            self.clearance,
            vertical_centre - self.clearance,
            -self.corridor_half_height + self.clearance,
            self.corridor_half_height - self.clearance,
        )

    def point(self, number: int) -> Point:
        """Stillages run clockwise: upper arm, end, then lower arm."""
        if not 1 <= number <= self.count:
            raise ValueError(f"Stillage {number} outside 1..{self.count}")
        p, w, h = self.pitch, self.width, self.corridor_half_height
        if number <= 2 * self.n:
            column = (number - 1) // 2
            return (column * p, h)
        if number <= 2 * self.n + 2 * self.m:
            offset = number - 2 * self.n - 1
            shelf_end = self.n * w + (self.n - 1) * self.gap
            return (shelf_end - w / 2, (self.m - 1) * p / 2 - (offset // 2) * p)
        offset = number - 2 * self.n - 2 * self.m - 1
        column = self.n - 1 - offset // 2
        return (column * p, -h)

    def contains(self, point: Point) -> bool:
        xmin, xmax, ymin, ymax = self.bounds
        return (
            xmin - 1e-9 <= point[0] <= xmax + 1e-9
            and ymin - 1e-9 <= point[1] <= ymax + 1e-9
        )


@dataclass(frozen=True)
class Instance:
    name: str
    layout: Layout
    qty_by_location: dict[int, int]  # requested storage location -> quantity
    capacity: int = 15
    nu: float = 1 / 3

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.nu < 0:
            raise ValueError("Invalid capacity or forklift factor")
        if any(
            not 1 <= k <= self.layout.count or not 1 <= v <= self.capacity
            for k, v in self.qty_by_location.items()
        ):
            raise ValueError("Invalid stillage or requested quantity")

    @property
    def requested_locations(self) -> tuple[int, ...]:
        """The set K of requested storage locations, in stable order."""
        return tuple(sorted(self.qty_by_location))


def load_instance(
    name: str = "44-(9x4)-10-01",
    path: str | Path = DEFAULT_DATA,
    capacity: int = 15,
    nu: float = 1 / 3,
) -> Instance:
    with Path(path).open(newline="", encoding="utf-8") as source:
        rows = (
            row
            for row in csv.DictReader(source, delimiter="\t")
            if row["label"] == name
        )
        row = next(rows, None)
    if row is None:
        raise KeyError(f"Instance {name!r} not found in {path}")
    layout = Layout(int(row["n"]), int(row["m"]))
    flags = ast.literal_eval(row["picklist"])
    weights = ast.literal_eval(row["weights"])
    assignment = ast.literal_eval(row["assignment_for_random_storage_policy"])
    if not (len(flags) == len(weights) == len(assignment) == layout.count):
        raise ValueError("Dataset vectors do not match the layout")
    if sorted(assignment) != list(range(1, layout.count + 1)) or any(
        f not in (0, 1) for f in flags
    ):
        raise ValueError("Invalid picklist or storage assignment")
    if sum(flags) != int(row["|Omega|"]):
        raise ValueError("Declared number of picks does not match picklist")
    qty_by_location = {
        assignment[i]: int(weights[i]) for i, flag in enumerate(flags) if flag
    }
    return Instance(name, layout, qty_by_location, capacity, nu)
