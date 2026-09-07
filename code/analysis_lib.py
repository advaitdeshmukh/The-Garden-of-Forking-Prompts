"""Analysis functions for the WildStories and WildEdits release."""
from __future__ import annotations

import re
from collections import defaultdict

import numpy as np
import pandas as pd


TARGET_ORDER = [
    "plot", "dialogue", "setting", "character description", "character name",
    "character gender", "character culture", "character substitution", "backstory",
    "fandom", "wording", "genre/style", "model instructions", "structure",
]
DIRECTION_ORDER = ["ADD", "REMOVE", "CHANGE", "EXTEND"]
MODE_ORDER = ["prose", "roleplay", "script", "narration"]

# Palettes and support thresholds.
TRANSITION_CMAP = "Greens"
PMI_COLORS = ["#54278F", "#FFFFFF", "#C51B7D"]
MIN_SUPPORT_USERS = 20
MIN_SUPPORT_STRATA = 20
RESEND_WINDOW_MINUTES = 120

# Display labels and row order.
TARGET_LABELS = {
    "plot": "Plot", "dialogue": "Dialogue", "setting": "Setting",
    "character description": "Character\nDescription",
    "character name": "Character\nName",
    "character gender": "Character\nGender",
    "character culture": "Character\nCulture",
    "character substitution": "Character\nSubstitution",
    "backstory": "Backstory", "fandom": "Fandom", "wording": "Wording",
    "genre/style": "Genre/Style",
    "model instructions": "Model\nInstructions", "structure": "Structure",
}
MODE_SPECS = [("Prose", "prose"), ("Roleplay", "roleplay"),
              ("Script", "script"), ("Narration", "narration")]
COMPONENT_SPECS = [("Instructions", "instructions"), ("Jailbreak", "jailbreak"),
                   ("Story stub", "story_stub"), ("Premise", "premise"),
                   ("Story summary", "story_summary"), ("Example", "example")]


def pmi_cmap():
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list("PurpleWhitePinkPMI", PMI_COLORS)


# --------------------------------------------------------------------------- data


def in_scope_pairs(edges: pd.DataFrame, trees: pd.DataFrame) -> pd.DataFrame:
    """In-scope edit pairs labelled with their (inferred user, source cluster) stratum.

    `source_cluster_id` is the pre-pruning cluster, so a cluster split into several
    trees by pruning remains a single stratum.
    """
    labelled = edges.merge(
        trees[["tree_id", "inferred_user_id", "source_cluster_id"]],
        on="tree_id", validate="many_to_one",
    )
    return labelled.loc[labelled.edge_status.eq("in_scope")].copy()


# ------------------------------------------------------------------ estimators


def pair_labels(actions: pd.DataFrame, column: str, order: list[str]) -> dict:
    valid = actions.loc[actions[column].isin(order)]
    key = (lambda values: tuple(sorted(set(values), key=order.index))) if column != "target" \
        else (lambda values: tuple(sorted(set(values))))
    return valid.groupby("pair_id")[column].apply(key).to_dict()


def transition_counts(pairs: pd.DataFrame, labels_by_pair: dict, labels: list[str]) -> dict:
    """Consecutive edits: the child prompt of one edit is the parent of the next.

    A pair carrying several labels splits its weight evenly across label
    combinations, so each transition contributes a total mass of one.
    """
    index = {label: i for i, label in enumerate(labels)}
    result = {}
    for key, group in pairs.groupby(["inferred_user_id", "source_cluster_id"]):
        by_parent = defaultdict(list)
        for row in group.itertuples(index=False):
            by_parent[row.parent_prompt_id].append(row)
        counts = np.zeros((len(labels), len(labels)))
        for row in group.itertuples(index=False):
            sources = labels_by_pair.get(row.pair_id, ())
            if not sources:
                continue
            for following in by_parent.get(row.child_prompt_id, []):
                destinations = labels_by_pair.get(following.pair_id, ())
                if not destinations:
                    continue
                weight = 1 / (len(sources) * len(destinations))
                for source in sources:
                    for destination in destinations:
                        counts[index[source], index[destination]] += weight
        if counts.sum():
            result[key] = counts
    return result


def pooled_matrix(stratum_counts: dict, labels: list[str]):
    """Every stratum's counts summed, then divided by the row total.

    The row total is the transition mass leaving a label, so a cell is a
    transition probability.

    Returns the matrix and each row's divisor.
    """
    total = np.zeros((len(labels), len(labels)), dtype=float)
    for counts in stratum_counts.values():
        total += counts
    divisor = total.sum(axis=1).astype(float)
    values = np.divide(
        total,
        divisor[:, None],
        out=np.full(total.shape, np.nan, dtype=float),
        where=divisor[:, None] > 0,
    )
    return (pd.DataFrame(values, index=labels, columns=labels),
            pd.Series(divisor, index=labels, name="row_denominator"))


def target_pmi(pairs: pd.DataFrame, labels_by_pair: dict):
    """Symmetric within-pair target PMI, log2(P(A,B) / (P(A) P(B))).

    Every labelled pair gets weight one, and no support mask is applied.

    Returns the matrix, the plotting mask, and the per-cell support counts.
    """
    from itertools import combinations

    labelled = pairs.loc[pairs.pair_id.isin(labels_by_pair)].copy()
    labelled["weight"] = 1.0

    weights = labelled[["pair_id", "weight", "inferred_user_id", "source_cluster_id"]]
    total = weights.weight.sum()
    single, joint = [], []
    for row in weights.itertuples(index=False):
        found = labels_by_pair[row.pair_id]
        single.extend({"pair_id": row.pair_id, "target": t} for t in found)
        joint.extend(
            {"pair_id": row.pair_id, "target_a": a, "target_b": b,
             "inferred_user_id": row.inferred_user_id,
             "source_cluster_id": row.source_cluster_id}
            for a, b in combinations(found, 2)
        )
    single = pd.DataFrame(single).merge(weights[["pair_id", "weight"]], on="pair_id")
    joint = pd.DataFrame(joint).merge(weights[["pair_id", "weight"]], on="pair_id")
    marginal = single.groupby("target").weight.sum()
    stats = joint.groupby(["target_a", "target_b"]).agg(
        mass=("weight", "sum"),
        pairs=("pair_id", "nunique"),
        users=("inferred_user_id", "nunique"),
        strata=("source_cluster_id", "nunique"),
    ).reset_index()

    size = len(TARGET_ORDER)
    matrix = np.full((size, size), np.nan)
    support = {name: np.zeros((size, size)) for name in ("pairs", "users", "strata")}
    index = {target: i for i, target in enumerate(TARGET_ORDER)}
    for row in stats.itertuples(index=False):
        value = np.log2(
            (row.mass / total)
            / ((marginal[row.target_a] / total) * (marginal[row.target_b] / total))
        )
        i, j = index[row.target_a], index[row.target_b]
        matrix[i, j] = matrix[j, i] = value
        for name, amount in (("pairs", row.pairs), ("users", row.users), ("strata", row.strata)):
            support[name][i, j] = support[name][j, i] = amount
    frame = pd.DataFrame(matrix, index=TARGET_ORDER, columns=TARGET_ORDER)
    mask = np.zeros((size, size), dtype=bool)
    return frame, mask, support


# -------------------------------------------------------------------- plotting


def heatmap(frame: pd.DataFrame, cmap: str, mask=None, figsize=None):
    """Conditional-rate heatmap."""
    import matplotlib.pyplot as plt
    import seaborn as sns

    values = frame.to_numpy()
    no_data = ~np.isfinite(values) | np.isclose(values, 0)
    plot_mask = no_data if mask is None else (np.asarray(mask) | no_data)
    annotations = np.full(values.shape, "", dtype=object)
    visible = ~plot_mask
    annotations[visible] = [
        f"{value:.2f}" if value >= 0.005 else "<.01" for value in values[visible]
    ]
    many = len(frame.index) > 6
    fig, ax = plt.subplots(figsize=figsize or ((12.5, 11) if many else (7.25, 6.4)))
    sns.heatmap(
        frame, mask=plot_mask, annot=annotations, fmt="", cmap=cmap,
        linewidths=0.35, annot_kws={"fontsize": 14.5 if many else 19},
        square=True, cbar=False, ax=ax,
    )
    _style_axes(ax, 16 if many else 20)
    plt.tight_layout(pad=0.25)
    return fig


def pmi_heatmap(frame: pd.DataFrame, mask=None):
    """Diverging PMI heatmap centred at zero."""
    import matplotlib.pyplot as plt
    import seaborn as sns

    values = frame.to_numpy()
    plot_mask = ~np.isfinite(values) | np.eye(len(frame.index), dtype=bool)
    if mask is not None:
        plot_mask = plot_mask | np.asarray(mask)
    finite = values[~plot_mask]
    limit = np.max(np.abs(finite)) if finite.size else 1
    annotations = np.full(values.shape, "", dtype=object)
    visible = ~plot_mask
    annotations[visible] = [f"{value:+.2f}" for value in values[visible]]
    fig, ax = plt.subplots(figsize=(12.5, 11))
    sns.heatmap(
        frame, mask=plot_mask, annot=annotations, fmt="", cmap=pmi_cmap(),
        center=0, vmin=-limit, vmax=limit, square=True, linewidths=0.35,
        annot_kws={"fontsize": 14.5}, cbar=False, ax=ax,
    )
    _style_axes(ax, 16)
    plt.tight_layout(pad=0.25)
    return fig


def _style_axes(ax, tick_size: int) -> None:
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.tick_params(axis="x", labelsize=tick_size, rotation=45)
    ax.tick_params(axis="y", labelsize=tick_size, rotation=0)
    for label in ax.get_xticklabels():
        label.set_horizontalalignment("right")
        label.set_rotation_mode("anchor")


# ------------------------------------------------------- refusal and resends


def response_opportunities(stories: pd.DataFrame, nodes: pd.DataFrame) -> pd.DataFrame:
    """One row per story prompt, joined to the next prompt in the same component.

    The follow-up is the next prompt in the component by `order_index`, not the
    prompt's inferred child: a user may return to another branch after a response.
    Prompts outside any edit tree are singleton components; they form their own
    component and have no follow-up, and they stay in the denominator.
    """
    columns = ["prompt_id", "tree_id", "order_index", "minutes_from_root",
               "normalized_text_id", "conversation_id"]
    frame = stories.merge(nodes[columns], on="prompt_id", how="left", suffixes=("", "_node"))
    # One string key for both populations. 
    singleton = frame.tree_id.isna()
    frame["tree_id"] = np.where(
        singleton,
        "singleton_" + frame.prompt_id.astype(str),
        "tree_" + frame.tree_id.fillna(-1).astype("int64").astype(str),
    )
    frame["order_index"] = frame.order_index.fillna(0)

    frame = frame.sort_values(["tree_id", "order_index"])
    grouped = frame.groupby("tree_id", sort=False)
    for column, name in [
        ("minutes_from_root", "next_minutes"), ("normalized_text_id", "next_normalized_text_id"),
        ("response_refusal", "next_refusal"), ("conversation_id", "next_conversation_id"),
    ]:
        frame[name] = grouped[column].shift(-1)
    frame["delay_minutes"] = (frame.next_minutes - frame.minutes_from_root).clip(lower=0)
    frame["followup_in_window"] = (
        frame.next_minutes.notna() & frame.delay_minutes.le(RESEND_WINDOW_MINUTES)
    )
    frame["normalized_resend"] = (
        frame.followup_in_window & frame.next_normalized_text_id.eq(frame.normalized_text_id)
    )
    return frame


def paired_within_user(frame: pd.DataFrame, value: str, split: str, unit: str = "tree_id"):
    """Average within component, then within user, keeping users present on both sides."""
    per_unit = frame.groupby(["inferred_user_id", split, unit])[value].mean().reset_index()
    return per_unit.groupby(["inferred_user_id", split])[value].mean().unstack().dropna()


# --------------------------------------------------- attribute-by-target PMI


def target_presence(pairs: pd.DataFrame, actions: pd.DataFrame) -> np.ndarray:
    """Binary target-presence matrix, one row per pair in `pairs` order."""
    labelled = actions.loc[actions.target.isin(TARGET_ORDER)].assign(present=1)
    table = labelled.pivot_table(
        index="pair_id", columns="target", values="present", aggfunc="max", fill_value=0
    ).reindex(columns=TARGET_ORDER, fill_value=0)
    return table.reindex(pairs.pair_id).fillna(0).to_numpy(dtype=float)


def attribute_target_pmi(attributes: pd.DataFrame, presence: np.ndarray, specs: list) -> pd.DataFrame:
    """PMI between a parent-prompt attribute and each edit target.

    Every eligible pair is counted once. Cells with no joint observation are left
    empty rather than reported as -inf.
    """
    marginal = presence.mean(axis=0)
    rows = []
    for _label, column in specs:
        attribute = (
            attributes["mode"].eq(column) if column in dict(MODE_SPECS).values()
            else attributes[column].astype(bool)
        ).to_numpy(dtype=float)
        joint = (presence * attribute[:, None]).mean(axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            value = np.log2(joint / (attribute.mean() * marginal))
        rows.append(np.where(joint > 0, value, np.nan))
    return pd.DataFrame(rows, index=[label for label, _ in specs], columns=TARGET_ORDER)


def attribute_pmi_heatmap(matrix: pd.DataFrame):
    """Attribute-by-target PMI heatmap with a symmetric symlog scale."""
    import matplotlib.pyplot as plt
    import seaborn as sns
    from matplotlib.colors import SymLogNorm

    sns.set_theme(context="paper", style="white", font_scale=1.05)
    fig, ax = plt.subplots(figsize=(12.6, 2.0 + 0.50 * len(matrix)))
    annotations = matrix.apply(
        lambda column: column.map(lambda v: "" if pd.isna(v) else f"{v:.2f}")
    )
    limit = np.nanmax(np.abs(matrix.to_numpy()))
    sns.heatmap(
        matrix, annot=annotations, fmt="", annot_kws={"fontsize": 14},
        cmap=pmi_cmap(),
        norm=SymLogNorm(linthresh=1.0, linscale=1.0, vmin=-limit, vmax=limit, base=10),
        xticklabels=[TARGET_LABELS[t] for t in matrix.columns],
        yticklabels=list(matrix.index),
        linewidths=0.15, linecolor="white", cbar=False, ax=ax,
    )
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.tick_params(axis="x", labelsize=14)
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", rotation_mode="anchor")
    ax.tick_params(axis="y", rotation=0, labelsize=15)
    fig.tight_layout()
    return fig


# ------------------------------------------------------- in-scope components

FIGURE9_GREEN = "#72B58A"


def inscope_components(
    edges: pd.DataFrame, trees: pd.DataFrame, min_nodes: int = 5
) -> pd.DataFrame:
    """Connected components of the in-scope edit graph, with at least `min_nodes` nodes.

    A released tree can contain no-marked-diff and out-of-scope edges; dropping
    those splits it into several components. Each component is keyed by its root:
    the parentless node earliest in source order.

    Source order here is `int(prompt_id)`, the WildChat turn identifier, which
    increases with turn creation across the corpus. It is *not*
    `wildedits_nodes.order_index`: that is numbered within a final pruned tree, so
    two components of one source cluster both start at zero. Component enumeration
    decides the `cluster_id` values and `sample_one_tree_per_user` sorts on those,
    so the wrong order silently changes which trees the seeded draw selects.
    """
    labelled = edges.merge(
        trees[["tree_id", "source_cluster_id"]], on="tree_id", validate="many_to_one"
    )
    keep = labelled.loc[
        labelled.edge_status.eq("in_scope") & labelled.parent_similarity.ge(0.5)
    ]
    def source_order(prompt):
        try:
            return (0, int(prompt))
        except (TypeError, ValueError):
            return (1, 0)

    rows = []
    for cluster_id, group in keep.groupby("source_cluster_id"):
        adjacency = defaultdict(set)
        has_parent = set()
        for row in group.itertuples(index=False):
            adjacency[row.parent_prompt_id].add(row.child_prompt_id)
            adjacency[row.child_prompt_id].add(row.parent_prompt_id)
            has_parent.add(row.child_prompt_id)
        seen = set()
        index = 0
        for start in sorted(adjacency, key=lambda p: (source_order(p), str(p))):
            if start in seen:
                continue
            component = {start}
            seen.add(start)
            stack = [start]
            while stack:
                node = stack.pop()
                for neighbour in adjacency[node]:
                    if neighbour not in seen:
                        seen.add(neighbour)
                        component.add(neighbour)
                        stack.append(neighbour)
            if len(component) < min_nodes:
                index += 1
                continue
            roots = sorted(
                (p for p in component if p not in has_parent),
                key=lambda p: (source_order(p), str(p)),
            )
            if roots:
                rows.append({
                    "source_cluster_id": cluster_id,
                    "component_index": index,
                    "cluster_id": f"{cluster_id}:{index}",
                    "root_prompt_id": roots[0],
                    "n_nodes": len(component),
                })
            index += 1
    return pd.DataFrame(rows)


def sample_one_tree_per_user(components: pd.DataFrame, seed: int = 42) -> pd.DataFrame:
    """Draw one component per inferred user.

    Users are visited in identifier order and each user's components are ordered
    by cluster id, so the draw depends only on the identifiers and the seed.
    """
    import random

    grouped = defaultdict(list)
    for position, row in enumerate(components.itertuples(index=False)):
        grouped[str(row.inferred_user_id)].append((row.cluster_id, position))
    rng = random.Random(seed)
    picked = [rng.choice(sorted(grouped[user]))[1] for user in sorted(grouped)]
    return components.iloc[sorted(picked)]


def tree_size_boxplot(frame: pd.DataFrame, xlim=(4.5, 20.5), xticks=(5, 8, 11, 14, 17, 20)):
    """Tree size by requested story format."""
    import matplotlib.pyplot as plt
    import seaborn as sns

    labels = {"prose": "Prose", "roleplay": "Roleplay", "script": "Script",
              "narration": "Narration"}
    plot_frame = frame.assign(Story_mode=frame["root_mode"].map(labels))
    sns.set_theme(context="paper", style="white", font_scale=1.15)
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    sns.boxplot(
        data=plot_frame, x="n_nodes", y="Story_mode",
        order=[labels[mode] for mode in MODE_ORDER],
        color=FIGURE9_GREEN, showfliers=False, whis=1.5,
        width=0.55, linewidth=1.25, ax=ax,
    )
    ax.set_xlim(*xlim)
    ax.set_xticks(list(xticks))
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.tick_params(axis="both", labelsize=14)
    ax.grid(axis="x", color="#dddddd", linewidth=0.7)
    ax.grid(axis="y", visible=False)
    sns.despine(ax=ax, left=True, bottom=False)
    fig.tight_layout()
    return fig


# --------------------------------------------------------------- edit volume

EDIT_VOLUME_COLORS = ("#90D8FF", "#C7EBFF", "#B8E6D0")
TOP_VOLUME_TARGETS = 10


def edit_volume_figure(actions: pd.DataFrame, pair_ratio: pd.DataFrame):
    """Edit-volume panels: net change by target and direction, and add ratio.

    `actions` needs `target`, `direction` and `net_change`, the latter in characters
    added minus removed over an action's spans. `pair_ratio` needs `direction` and
    `add_ratio` per pair-direction combination; a pair carrying several directions
    appears under each. Actions with no changed characters are excluded.
    """
    import matplotlib.pyplot as plt
    import seaborn as sns
    from matplotlib.ticker import NullFormatter, ScalarFormatter

    volume = actions.loc[actions.edit_volume > 0]
    top = volume.target.value_counts().head(TOP_VOLUME_TARGETS).index
    by_target = volume.loc[volume.target.isin(top)]
    order = by_target.groupby("target").net_change.median().sort_values(
        ascending=False).index.tolist()

    def signed_axis(ax):
        ax.set_xscale("symlog", linthresh=10, linscale=1)
        ax.xaxis.set_major_formatter(ScalarFormatter())
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.axvline(0, color="grey", linewidth=1.1, linestyle="--", zorder=0)

    fig = plt.figure(figsize=(14, 12), constrained_layout=True)
    grid = fig.add_gridspec(2, 2, height_ratios=[1.25, 1])
    axes = [fig.add_subplot(grid[0, :]), fig.add_subplot(grid[1, 0]),
            fig.add_subplot(grid[1, 1])]
    label = "Edit Volume (− characters removed, + added)"
    sns.boxplot(data=by_target, y="target", x="net_change", order=order,
                ax=axes[0], color=EDIT_VOLUME_COLORS[0], showfliers=False)
    sns.boxplot(data=volume, y="direction", x="net_change", order=DIRECTION_ORDER,
                ax=axes[1], color=EDIT_VOLUME_COLORS[1], showfliers=False)
    for ax in axes[:2]:
        signed_axis(ax)
        ax.set_xlabel(label, fontsize=22)
        ax.set_ylabel("")
    # Full-range whiskers: the ratio panel shows the whole span of pair ratios,
    # so the tails at 0 and 1 stay visible instead of being cut at 1.5 IQR.
    sns.boxplot(data=pair_ratio, y="direction", x="add_ratio", order=DIRECTION_ORDER,
                ax=axes[2], color=EDIT_VOLUME_COLORS[2], showfliers=False,
                whis=(0, 100))
    axes[2].axvline(0.5, color="grey", linewidth=0.8, linestyle="--", zorder=0)
    axes[2].set_xlim(-0.05, 1.05)
    axes[2].set_xlabel("Ratio (0 = Remove, 1 = Add)", fontsize=22)
    axes[2].set_ylabel("")
    for ax in axes:
        ax.tick_params(axis="both", labelsize=16)
    return fig


# ----------------------------------------------------------- word-level PMI

# Span tokens: letters only, with apostrophes and hyphens allowed inside a word.
SPAN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")
N_BOOTSTRAP = 300
BOOTSTRAP_SEED = 42
TOP_PMI_TARGETS = 10
TOP_PMI_WORDS = 10


def span_words(text: str) -> list[str]:
    """Presence/absence word features of one action's span text."""
    return sorted(set(SPAN_TOKEN_RE.findall(str(text or "").lower())))


def action_weights(actions: pd.DataFrame) -> pd.DataFrame:
    """Weight each action by 1 / (clusters for its user x actions in its stratum).

    Each user's total mass is one and their clusters divide it equally.
    """
    frame = actions.copy()
    clusters_per_user = frame.groupby("inferred_user_id").source_cluster_id.nunique()
    per_stratum = frame.groupby(
        ["inferred_user_id", "source_cluster_id"]
    ).action_index.transform("size")
    frame["weight"] = 1 / (frame.inferred_user_id.map(clusters_per_user) * per_stratum)
    frame["stratum"] = list(zip(frame.inferred_user_id, frame.source_cluster_id))
    return frame


def hierarchical_target_order(actions: pd.DataFrame) -> list[str]:
    """Targets ranked by user- and cluster-balanced frequency."""
    counts = actions.groupby(
        ["inferred_user_id", "source_cluster_id", "target"]
    ).size().rename("n").reset_index()
    counts["p"] = counts.n / counts.groupby(
        ["inferred_user_id", "source_cluster_id"]
    ).n.transform("sum")
    strata = counts.pivot_table(
        index=["inferred_user_id", "source_cluster_id"], columns="target",
        values="p", fill_value=0,
    ).reindex(columns=TARGET_ORDER, fill_value=0)
    return (
        strata.groupby(level="inferred_user_id").mean().mean()
        .sort_values(ascending=False).index.tolist()
    )


def weighted_word_pmi(weighted: pd.DataFrame) -> pd.DataFrame:
    """PMI between span words and edit targets, weighted by user and cluster.

    A word and a word-target pair each need at least 20 supporting users and 20
    supporting user-clusters to be reported.
    """
    total = weighted.weight.sum()
    rows = weighted[
        ["inferred_user_id", "stratum", "target", "weight", "words"]
    ].explode("words").dropna(subset=["words"])
    stats = rows.groupby("words").agg(
        word_weight=("weight", "sum"),
        users=("inferred_user_id", "nunique"),
        user_clusters=("stratum", "nunique"),
    )
    eligible = stats[
        (stats.users >= MIN_SUPPORT_USERS) & (stats.user_clusters >= MIN_SUPPORT_STRATA)
    ].index
    rows = rows[rows.words.isin(eligible)]
    joint = rows.groupby(["words", "target"]).agg(
        joint_weight=("weight", "sum"),
        users=("inferred_user_id", "nunique"),
        user_clusters=("stratum", "nunique"),
    ).reset_index()
    joint = joint[
        (joint.users >= MIN_SUPPORT_USERS) & (joint.user_clusters >= MIN_SUPPORT_STRATA)
    ]
    target_weight = weighted.groupby("target").weight.sum()
    joint["pmi"] = np.log2(
        (joint.joint_weight / total)
        / ((joint.words.map(stats.word_weight) / total)
           * (joint.target.map(target_weight) / total))
    )
    return joint


def bootstrap_word_pmi(weighted: pd.DataFrame, top: pd.DataFrame,
                       targets: list[str], user_order: dict[str, int]) -> pd.DataFrame:
    """Resample complete users 300 times, retaining all of a sampled user's actions.

    `user_order` fixes the order of the user list: the multinomial draw is indexed
    by position, so the order decides which draw lands on which user.
    """
    users = sorted(weighted.inferred_user_id.unique(), key=lambda u: user_order[u])
    words = sorted(top.words.unique())
    target_mass = weighted.groupby(
        ["inferred_user_id", "target"]
    ).weight.sum().unstack(fill_value=0).reindex(index=users, columns=targets, fill_value=0)

    exploded = weighted[
        ["inferred_user_id", "target", "weight", "words"]
    ].explode("words").dropna(subset=["words"])
    exploded = exploded[exploded.words.isin(words)]
    word_mass = exploded.groupby(
        ["inferred_user_id", "words"]
    ).weight.sum().unstack(fill_value=0).reindex(index=users, columns=words, fill_value=0)

    user_index = {u: i for i, u in enumerate(users)}
    word_index = {w: i for i, w in enumerate(words)}
    target_index = {t: i for i, t in enumerate(targets)}
    joint_mass = np.zeros((len(users), len(words), len(targets)))
    grouped = exploded[exploded.target.isin(targets)].groupby(
        ["inferred_user_id", "words", "target"], as_index=False
    ).weight.sum()
    for row in grouped.itertuples(index=False):
        joint_mass[user_index[row.inferred_user_id], word_index[row.words],
                   target_index[row.target]] = row.weight

    n = len(users)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    multiplicity = rng.multinomial(n, np.full(n, 1 / n), size=N_BOOTSTRAP)
    target_boot = multiplicity @ target_mass.to_numpy() / n
    word_boot = multiplicity @ word_mass.to_numpy() / n
    joint_boot = (multiplicity @ joint_mass.reshape(n, -1) / n).reshape(
        N_BOOTSTRAP, len(words), len(targets)
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        draws = np.log2(joint_boot / (word_boot[:, :, None] * target_boot[:, None, :]))

    out = []
    for row in top.itertuples(index=False):
        values = draws[:, word_index[row.words], target_index[row.target]]
        finite = values[np.isfinite(values)]
        out.append({
            "target": row.target, "word": row.words, "point_pmi": row.pmi,
            "bootstrap_mean_pmi": finite.mean(),
            "bootstrap_sd_pmi": finite.std(ddof=1),
        })
    return pd.DataFrame(out)
