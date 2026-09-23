"""Building blocks of the dashboard's "Night drive" look, and the callbacks drawing its summary.

The summary's figures are computed in the browser (assets/clientside.js) from the stores the page
already holds, like the rest of the dashboard, so choosing a period doesn't go back to the server.
"""
from dash import dcc, html
from dash.dependencies import Input, Output, State
import dash_bootstrap_components as dbc

from psa_car_controller.web import figures

# (id suffix, label, days back from today; None: everything)
PERIODS = [("7", "7 days", 7), ("30", "30 days", 30), ("365", "Year", 365), ("all", "All", None)]
PERIOD_BUTTONS = [f"period-{key}" for key, _, _ in PERIODS] + ["period-custom"]


def icon(name, small=False):
    """An icon from assets/icons, painted in the text colour."""
    return html.Span(className=f"psacc-icon psacc-icon-{name}" + (" psacc-icon-sm" if small else ""),
                     **{"aria-hidden": "true"})


def card(children, class_name="", **kwargs):
    return html.Div(children, className=("psacc-card " + class_name).strip(), **kwargs)


def tile(label, icon_name, value_id, unit, sub, *, tile_id=None, empty=None):  # pylint: disable=too-many-arguments
    """A total: a label, a big number (filled in the browser) and a line under it."""
    children = [
        html.Div([html.Span(label), icon(icon_name)], className="psacc-tile-head"),
        html.Div([html.Span("–", id=value_id, className="num psacc-tile-value"), html.Span(unit)],
                 className="psacc-tile-main"),
        html.Div(sub, className="psacc-tile-sub"),
    ]
    if empty:  # shown instead when there is nothing to total
        children.append(html.Div(empty, className="psacc-tile-empty-text"))
    kwargs = {"id": tile_id} if tile_id else {}
    return card(children, "psacc-tile", **kwargs)


def summary_tiles():
    currency = figures.CURRENCY
    return html.Section(className="psacc-tiles", **{"aria-label": "Totals"}, children=[
        tile("Consumption", "gauge", figures.AVG_CONSUM_KW, "kWh/100 km",
             [html.Span("–", id=figures.AVG_CONSUM_PRICE, className="num psacc-strong"),
              f" {currency} per 100 km"]),
        tile("Electricity used", "plug", figures.ELEC_CONSUM_KW, "kWh",
             [html.Span("–", id=figures.ELEC_CONSUM_PRICE, className="num psacc-strong"),
              f" {currency} at your tariff"]),
        tile("Charge speed", "bolt", figures.AVG_CHARGE_SPEED, "kW", "average over the period"),
        tile("CO₂", "cloud", figures.AVG_EMISSION_KM, "g/km",
             [html.Span("–", id=figures.AVG_EMISSION_KW, className="num psacc-strong"), " g/kWh charged"],
             tile_id="tile-co2", empty=[html.Div("No data", className="psacc-tile-empty-value"),
                                        html.Div("No CO₂ figures for these charges", className="psacc-tile-sub")]),
    ])


def period_picker(range_slider):
    """Quick periods, and the date slider for any other one."""
    buttons = [html.Button(label, id=f"period-{key}", type="button", n_clicks=0, className="psacc-seg")
               for key, label, _ in PERIODS]
    buttons.append(html.Button("Custom…", id="period-custom", type="button", n_clicks=0, className="psacc-seg"))
    return html.Div(className="psacc-period", children=[
        html.Div(className="psacc-toolbar", children=[
            html.Div(buttons, role="group", className="psacc-segmented", **{"aria-label": "Period"}),
            html.Span([icon("calendar", small=True), html.Span(id="period-label", className="num")],
                      className="psacc-period-label"),
        ]),
        dcc.Store(id="period-store", storage_type="local"),
        dbc.Collapse(html.Div(range_slider, className="psacc-custom-range"), id="period-custom-range",
                     is_open=False),
    ])


def summary_panel(graphs):
    """The summary: totals, the last day driven, the latest figures, then the consumption graphs."""
    consumption, by_speed, by_temp = graphs
    for graph in graphs:
        graph.config = {"displayModeBar": False}
    return [
        summary_tiles(),
        html.Section(className="psacc-grid-2-1", children=[
            card([
                html.Div([html.H2(id="day-battery-title", className="psacc-card-title"),
                          html.Div([html.Span([html.Span(className="psacc-swatch psacc-swatch-line"), "Battery"]),
                                    html.Span([html.Span(className="psacc-swatch psacc-swatch-drive"), "Driving"]),
                                    html.Span([html.Span(className="psacc-swatch psacc-swatch-charge"), "Charging"])],
                                   className="psacc-legend")],
                         className="psacc-card-head"),
                dcc.Graph(id="day-battery-graph", config={"displayModeBar": False}, style={"height": "260px"}),
            ]),
            card([
                html.Div([html.H2(id="day-trips-title", className="psacc-card-title"),
                          html.Div(id="day-trips-sub", className="psacc-card-sub")]),
                html.Div(id="day-trips-list", className="psacc-day-trips"),
                html.A("All trips →", href="#trips", className="psacc-card-link"),
            ], "psacc-day-card"),
        ]),
        html.Section(className="psacc-grid-3", children=[
            card(id="last-charge-card", children=[]),
            card(id="odometer-card", children=[]),
            card(id="fuel-card", children=[]),
        ]),
        html.Section(className="psacc-charts", children=[
            card(consumption, "psacc-chart-card psacc-chart-wide"),
            card(by_speed, "psacc-chart-card"),
            card(by_temp, "psacc-chart-card"),
        ]),
    ]


def register_callbacks(dash_app):
    """Registered once when the app starts, like FigureFilter's (see set_clientside_callback)."""
    dash_app.clientside_callback(
        "function(...clicks) { return psaccPeriodClicked() }",
        Output("period-store", "data"),
        [Input(button, "n_clicks") for button in PERIOD_BUTTONS],
        prevent_initial_call=True,
    )
    dash_app.clientside_callback(
        "function(period, min, max, value) { return psaccApplyPeriod(period, min, max, value) }",
        [Output("date-slider", "value"), Output("period-custom-range", "is_open")]
        + [Output(button, "className") for button in PERIOD_BUTTONS],
        Input("period-store", "data"),
        [State("date-slider", "min"), State("date-slider", "max"), State("date-slider", "value")],
    )
    dash_app.clientside_callback(
        "function(value) { return psaccPeriodLabel(value) }",
        Output("period-label", "children"),
        Input("date-slider", "value"),
    )
    dash_app.clientside_callback(
        "function(data, range, config) { return psaccSummaryDay(data, range, config) }",
        [Output("day-battery-graph", "figure"), Output("day-battery-title", "children"),
         Output("day-trips-title", "children"), Output("day-trips-sub", "children"),
         Output("day-trips-list", "children"), Output("last-charge-card", "children"),
         Output("odometer-card", "children"), Output("fuel-card", "children"), Output("fuel-card", "style")],
        [Input("clientside-data-store", "data"), Input("date-slider", "value")],
        State("clientside-config-store", "data"),
    )
