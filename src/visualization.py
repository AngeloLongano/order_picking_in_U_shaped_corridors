"""Plans and sampled-cost plots for the teaching notebook."""

from __future__ import annotations

import itertools

from matplotlib import pyplot as plt
from matplotlib.patches import Rectangle

from .basic import SearchResult
from .instances import Instance, Point
from .routing import RouteResult
from .zoomin import ZoomResult


def plot_layout(
    instance: Instance,
    depot: Point | None = None,
    route: RouteResult | None = None,
    candidates: tuple[Point, ...] = (),
    ax=None,
):
    if ax is None:
        _, ax = plt.subplots(figsize=(11, 5))
    layout = instance.layout
    w = layout.width
    for k in range(1, layout.count + 1):
        x, y = layout.point(k)
        # Paired stillages share a picking coordinate: draw both tiers separately.
        upper_arm = k <= 2 * layout.n
        lower_arm = k > 2 * layout.n + 2 * layout.m
        if upper_arm:
            rect = (x - w / 2, y + (0 if k % 2 else w / 2))
            size = (w, w / 2)
        elif lower_arm:
            rect = (x - w / 2, y - (w / 2 if k % 2 else w))
            size = (w, w / 2)
        else:
            rect = (x + (0 if k % 2 else w / 2), y - w / 2)
            size = (w / 2, w)
        ax.add_patch(
            Rectangle(
                rect,
                *size,
                edgecolor="#607080",
                facecolor="#f5a47a" if k in instance.qty_by_location else "#e7e9ec",
                lw=0.8,
            )
        )
        ax.text(
            rect[0] + size[0] / 2,
            rect[1] + size[1] / 2,
            str(k),
            ha="center",
            va="center",
            fontsize=6,
        )
    ax.axhline(0, color="#a5adb4", lw=0.7, ls="--")
    ax.scatter([0], [0], marker="s", color="black", label="Ingresso O", zorder=5)
    if candidates:
        ax.scatter(
            [p[0] for p in candidates],
            [p[1] for p in candidates],
            s=12,
            color="#55a6c9",
            alpha=0.55,
            label="Candidati",
            zorder=4,
        )
    if depot is not None:
        ax.scatter(
            [depot[0]],
            [depot[1]],
            marker="D",
            color="#176c3a",
            s=65,
            label="Deposito D",
            zorder=7,
        )
    if route is not None:
        colors = plt.get_cmap("tab10")
        for t, tour in enumerate(route.tours):
            points = [route.depot] + [layout.point(k) for k in tour] + [route.depot]
            for a, b in itertools.pairwise(points):
                ax.annotate(
                    "",
                    xy=b,
                    xytext=a,
                    arrowprops={"arrowstyle": "->", "color": colors(t), "lw": 1.7},
                )
    ax.set(xlabel="x (m)", ylabel="y (m)", title=f"Layout {instance.name}")
    ax.set_aspect("equal", adjustable="box")
    ax.legend(loc="lower left", fontsize=8)
    return ax


def plot_cost_map(
    search: SearchResult | ZoomResult,
    best_1d: Point | None = None,
    best_2d: Point | None = None,
    ax=None,
):
    if ax is None:
        _, ax = plt.subplots(figsize=(10, 4))
    positions: list[Point] = []
    costs: list[float] = []
    for candidate in search.candidates:
        if candidate.total_cost is not None:
            positions.append(candidate.depot)
            costs.append(candidate.total_cost)
    if positions:
        scatter = ax.scatter(
            [point[0] for point in positions],
            [point[1] for point in positions],
            c=costs,
            cmap="RdYlGn_r",
            s=52,
            marker="s",
            edgecolors="none",
        )
        plt.colorbar(scatter, ax=ax, label="Costo totale (m equivalenti)")
    if best_1d is not None:
        ax.scatter(*best_1d, marker="x", s=130, color="black", label="Migliore 1D")
    if best_2d is not None:
        ax.scatter(*best_2d, marker="*", s=160, color="#2653cf", label="Migliore 2D")
    if isinstance(search, ZoomResult):
        for n, scan in enumerate(search.scans):
            xmin, xmax, ymin, ymax = scan.bounds
            ax.add_patch(
                Rectangle(
                    (xmin, ymin),
                    xmax - xmin,
                    ymax - ymin,
                    fill=False,
                    ec="black",
                    lw=1,
                    alpha=0.4,
                )
            )
            ax.text(xmin, ymax, str(n + 1), fontsize=8)
    ax.set(xlabel="x (m)", ylabel="y (m)", title="Costi calcolati sulla griglia")
    ax.set_aspect("equal", adjustable="box")
    ax.legend(loc="best")
    return ax
