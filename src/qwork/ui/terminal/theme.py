"""ATAS-inspired dark presentation shared by terminal charts and tables."""

PALETTE = dict(
    background="#17191c",
    panel="#24272b",
    canvas="#1c1f22",
    chrome="#30343a",
    text="#d6d8dc",
    muted="#989da5",
    grid="#34383e",
    accent="#67add3",
    up="#63b89b",
    down="#d8777b",
    warning="#dbb96b",
)


GRID_THEME = {
    "function": "themeQuartz.withParams({backgroundColor:'#24272b', foregroundColor:'#d6d8dc', headerBackgroundColor:'#30343a', headerTextColor:'#b9bec5', oddRowBackgroundColor:'#282c31', accentColor:'#67add3', borderColor:'#3a3f46', fontFamily:'Segoe UI', fontSize:11, rowHeight:25, headerHeight:27, borderRadius:0, wrapperBorderRadius:0})"
}


def chart_theme(figure):
    figure.update_layout(
        paper_bgcolor=PALETTE["panel"],
        plot_bgcolor=PALETTE["canvas"],
        font=dict(family="Segoe UI, Microsoft YaHei, sans-serif", size=11, color=PALETTE["text"]),
        hoverlabel=dict(bgcolor=PALETTE["chrome"], bordercolor=PALETTE["grid"], font_color=PALETTE["text"]),
        legend=dict(font_size=11),
    )
    axis = dict(
        gridcolor=PALETTE["grid"],
        gridwidth=1,
        zeroline=False,
        linecolor=PALETTE["grid"],
        tickfont=dict(size=10, color=PALETTE["muted"]),
        title_font=dict(size=11),
        automargin=True,
        showspikes=True,
        spikethickness=1,
        spikedash="dot",
        spikecolor=PALETTE["muted"],
        spikesnap="cursor",
    )
    figure.update_xaxes(**axis)
    figure.update_yaxes(**axis)
    figure.update_scenes(bgcolor=PALETTE["canvas"])
    return figure
