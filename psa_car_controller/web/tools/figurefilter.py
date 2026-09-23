import json
import logging
from logging import DEBUG

from dash import dcc
from dash.dependencies import Output, Input, State

logger = logging.getLogger(__name__)


class Graph:
    def __init__(self, graph_id, x, y: [], figure):
        self.graph_id = graph_id
        self.x = x
        self.y = y
        self.figure = figure


class Table:
    def __init__(self, table_id, src, date_columns, figure):
        self.table_id = table_id
        self.src = src
        self.figure = figure
        # the columns shown as <column>_str, formatted in the browser's locale
        self.date_columns = date_columns


def figures_to_dict(figures):
    el_list = []
    for figure in figures:
        res = {}
        for key, value in figure.__dict__.items():
            if key != "figure":
                res[key] = value
        el_list.append(res)
    return el_list


class FigureFilter:

    def __init__(self):
        self.graphs = []
        self.tables = []
        self.maps = []
        self.src = {}
        # the datetime columns of the datasets no table shows
        self.dataset_dates = {}

    def add_map(self, dash_Graph, latitude, longitude, figure):
        self.maps.append(Graph(dash_Graph.id, latitude, longitude, figure))
        return dash_Graph

    def add_graph(self, dash_Graph, x, y, figure):
        self.graphs.append(Graph(dash_Graph.id, x, y, figure))
        return dash_Graph

    def add_table(self, table_id, src, date_columns, figure):
        self.tables.append(Table(table_id, src, date_columns, figure))

    def add_dataset_dates(self, src, date_columns):
        """Datetime columns to localise in a dataset that no table shows (the map reads them)."""
        self.dataset_dates[src] = date_columns

    def __get_table_date_column_id(self):
        res = dict(self.dataset_dates)
        res.update({table.src: table.date_columns for table in self.tables})
        return res

    def __get_figures(self):
        return {"graph": [graph.figure for graph in self.graphs],
                "tables": [table.figure for table in self.tables],
                "maps": [map.figure for map in self.maps]}

    def __get_output(self) -> list:
        outputs = [Output(table.table_id, "data") for table in self.tables]
        outputs.extend([Output(graph.graph_id, "figure") for graph in self.graphs])
        outputs.extend([Output(graph.graph_id, "figure") for graph in self.maps])
        return outputs

    @staticmethod
    def __get_graph_x_label(graphs):
        return [graph.x for graph in graphs]

    @staticmethod
    def __get_graph_y_label(graphs):
        return [graph.y for graph in graphs]

    def __get_table_input_sort_by(self):
        inputs = [Input(table.table_id, 'sort_by') for table in self.tables]
        return inputs

    def gen_sort_variable(self):
        res = ", ".join([chr(i) for i in range(ord('a'), ord('a') + len(self.tables))])
        return res

    def __gen_sort_dict(self):
        res = "{"
        i = ord("a")
        for table in self.tables:
            res += f'"{table.table_id}": {chr(i)},'
            i += 1
        res = res[:-1] + "}"
        return res

    def get_params(self):
        params = json.dumps({
            "date_columns": self.__get_table_date_column_id(),
            "table_src": figures_to_dict(self.tables),
            "graph_x_label": self.__get_graph_x_label(self.graphs),
            "graph_y_label": self.__get_graph_y_label(self.graphs),
            "map_x_label": self.__get_graph_x_label(self.maps),
            "map_y_label": self.__get_graph_y_label(self.maps)
        }, indent=4)
        return params

    def set_clientside_callback(self, dash_app):
        """Registers the callback filling the graphs, map and tables from the stores.

        It only depends on the ids and labels, so it's registered once when the app starts: the
        browser reads the callbacks before asking for the page, a callback registered while building
        the page would be missing on the first load.
        """
        inputs = [Input('clientside-data-store', 'data'),
                  Input('date-slider', 'value'),
                  Input('clientside-figure-store', 'data'),
                  *self.__get_table_input_sort_by()]
        log_level = 10 if logger.isEnabledFor(DEBUG) else 20
        fct_def = f"""function(data,range, figures, {self.gen_sort_variable()}, config) {{
                        const params={self.get_params()}
                        const logLevel={log_level}
                        return filterAndSort(data, range, figures, params, logLevel,
                         {self.__gen_sort_dict()}, config)
                      }}"""
        dash_app.clientside_callback(fct_def, *self.__get_output(), *inputs, State('clientside-config-store', 'data'))

    def get_store(self, config: dict):
        return [dcc.Store(id='clientside-figure-store', data=self.__get_figures()),
                dcc.Store(id='clientside-data-store', data=self.src),
                dcc.Store(id='clientside-config-store', data=config)]
