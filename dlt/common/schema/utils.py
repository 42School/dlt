from copy import deepcopy
import re
import base64
import binascii
import datetime
from dateutil.parser import isoparse
from typing import Dict, List, Sequence, Type, Any, cast

from dlt.common import pendulum, json, Decimal
from dlt.common.arithmetics import ConversionSyntax
from dlt.common.typing import DictStrAny
from dlt.common.validation import validate_dict
from dlt.common.schema.typing import TStoredSchema, TTable, TTableColumns, TColumnBase, TColumn, TColumnProp, TDataType, THintType
from dlt.common.schema.exceptions import SchemaEngineNoUpgradePathException


RE_LEADING_DIGITS = re.compile(r"^\d+")
RE_NON_ALPHANUMERIC = re.compile(r"[^a-zA-Z\d]")


# fix a name so it is acceptable as schema name
def normalize_schema_name(name: str) -> str:
    # prefix the name starting with digits
    if RE_LEADING_DIGITS.match(name):
        name = "s" + name
    # leave only alphanumeric
    return RE_NON_ALPHANUMERIC.sub("", name).lower()


def apply_defaults(stored_schema: TStoredSchema) -> None:
    for table_name, table in stored_schema["tables"].items():
        # overwrite name
        table["name"] = table_name
        # add default write disposition to root tables
        if table.get("parent") is None and table.get("write_disposition") is None:
            table["write_disposition"] = "append"
        # add missing hints to columns
        for column_name in table["columns"]:
            # add default hints to tables
            column = add_missing_hints(table["columns"][column_name])
            # overwrite column name
            column["name"] = column_name
            # set column with default
            table["columns"][column_name] = column


def remove_defaults(stored_schema: TStoredSchema) -> None:
    clean_tables = deepcopy(stored_schema["tables"])
    for t in clean_tables.values():
        del t["name"]
        for c in t["columns"].values():
            # do not save names
            del c["name"]  # type: ignore
            # remove hints with default values
            for h in list(c.keys()):
                if isinstance(c[h], bool) and c[h] is False and h != "nullable":  # type: ignore
                    del c[h]  # type: ignore

    stored_schema["tables"] = clean_tables


def validate_stored_schema(stored_schema: TStoredSchema) -> None:
    # verify only non extra fields
    validate_dict(TStoredSchema, stored_schema, ".", lambda k: not k.startswith("x-"))


def upgrade_engine_version(schema_dict: DictStrAny, from_engine: int, to_engine: int) -> TStoredSchema:
    if from_engine == to_engine:
        return cast(TStoredSchema, schema_dict)

    if from_engine == 1 and to_engine > 1:
        schema_dict["engine_version"] = 2
        schema_dict["includes"] = []
        schema_dict["excludes"] = []
        from_engine = 2
    if from_engine == 2 and to_engine > 2:
        # current version of the schema
        current = cast(TStoredSchema, schema_dict)
        # add default normalizers and root hash propagation
        current["normalizers"] = {
            "names": "dlt.common.normalizers.names.snake_case",
            "json": {
                "module": "dlt.common.normalizers.json.relational",
                "config": {
                    "propagation": {
                        "root": {
                            "_record_hash": "_root_hash"
                        }
                    }
                }
            }
        }
        # move settings
        current["settings"] = {
            "default_hints": schema_dict.pop("hints", {}),
            "preferred_types": schema_dict.pop("preferred_types", {}),
        }
        # repackage tables
        old_tables: Dict[str, TTableColumns] = schema_dict.pop("tables")
        current["tables"] = {}
        for name, columns in old_tables.items():
            # find last path separator
            parent = name
            # go back in a loop to find existing parent
            while True:
                idx = parent.rfind("__")
                if idx > 0:
                    parent = parent[:idx]
                    if parent not in old_tables:
                        # print(f"candidate {parent} for {name} not found")
                        continue
                else:
                    parent = None
                break
            nt = new_table(name, parent)
            nt["columns"] = columns
            current["tables"][name] = nt
        # assign exclude and include to tables

        def migrate_filters(group: str, filters: List[str]) -> None:
            # existing filter were always defined at the root table. find this table and move filters
            for f in filters:
                # skip initial ^
                root = f[1:f.find("__")]
                path = f[f.find("__") + 2:]
                t = current["tables"].get(root)
                if t is None:
                    # must add new table to hold filters
                    t = new_table(root)
                    current["tables"][root] = t
                t.setdefault("filters", {}).setdefault(group, []).append("^" + path)  # type: ignore

        excludes = schema_dict.pop("excludes", [])
        migrate_filters("excludes", excludes)
        includes = schema_dict.pop("includes", [])
        migrate_filters("includes", includes)

        # upgraded
        schema_dict["engine_version"] = 3
        from_engine = 3
    if from_engine == 3:
        pass
    if from_engine != to_engine:
        raise SchemaEngineNoUpgradePathException(schema_dict["name"], schema_dict["engine_version"], from_engine, to_engine)
    return cast(TStoredSchema, schema_dict)


def add_missing_hints(column: TColumnBase) -> TColumn:
    return {
        **{  # type:ignore
            "partition": False,
            "cluster": False,
            "unique": False,
            "sort": False,
            "primary_key": False,
            "foreign_key": False,
        },
        **column
    }


def py_type_to_sc_type(t: Type[Any]) -> TDataType:
    if t is float:
        return "double"
    elif t is int:
        return "bigint"
    elif t is bool:
        return "bool"
    elif issubclass(t, bytes):
        return "binary"
    elif issubclass(t, dict) or issubclass(t, list):
        return "complex"
    elif issubclass(t, Decimal):
        return "decimal"
    elif issubclass(t, datetime.datetime):
        return "timestamp"
    else:
        return "text"


def coerce_type(to_type: TDataType, from_type: TDataType, value: Any) -> Any:
    if to_type == from_type:
        if to_type == "complex":
            # complex types will be always represented as strings
            return json.dumps(value)
        return value

    if to_type == "text":
        if from_type == "complex":
            return json.dumps(value)
        else:
            return str(value)

    if to_type == "binary":
        if from_type == "text":
            if value.startswith("0x"):
                return bytes.fromhex(value[2:])
            try:
                return base64.b64decode(value, validate=True)
            except binascii.Error:
                raise ValueError(value)
        if from_type == "bigint":
            return value.to_bytes((value.bit_length() + 7) // 8, 'little')

    if to_type in ["wei", "bigint"]:
        if from_type == "bigint":
            return value
        if from_type in ["decimal", "double"]:
            if value % 1 != 0:
                # only integer decimals and floats can be coerced
                raise ValueError(value)
            return int(value)
        if from_type == "text":
            trim_value = value.strip()
            if trim_value.startswith("0x"):
                return int(trim_value[2:], 16)
            else:
                return int(trim_value)

    if to_type == "double":
        if from_type in ["bigint", "wei", "decimal"]:
            return float(value)
        if from_type == "text":
            trim_value = value.strip()
            if trim_value.startswith("0x"):
                return float(int(trim_value[2:], 16))
            else:
                return float(trim_value)

    if to_type == "decimal":
        if from_type in ["bigint", "wei"]:
            return value
        if from_type == "double":
            return Decimal(value)
        if from_type == "text":
            trim_value = value.strip()
            if trim_value.startswith("0x"):
                return int(trim_value[2:], 16)
            elif "." not in trim_value and "e" not in trim_value:
                return int(trim_value)
            else:
                try:
                    return Decimal(trim_value)
                except ConversionSyntax:
                    raise ValueError(trim_value)

    if to_type == "timestamp":
        if from_type in ["bigint", "double"]:
            # returns ISO datetime with timezone
            return str(pendulum.from_timestamp(value))

        if from_type == "text":
            # if parses as ISO date then pass it
            try:
                isoparse(value)
                return value
            except ValueError:
                # try to convert string to integer, or float
                try:
                    value = int(value)
                except ValueError:
                    # raises ValueError if not parsing correctly
                    value = float(value)
                return str(pendulum.from_timestamp(value))

    raise ValueError(value)


def compare_columns(a: TColumn, b: TColumn) -> bool:
    return a["data_type"] == b["data_type"] and a["nullable"] == b["nullable"]


def hint_to_column_prop(h: THintType) -> TColumnProp:
    if h == "not_null":
        return "nullable"
    return h


def version_table() -> TTable:
    return {
        "description": "Created by DLT. Tracks schema updates",
        "write_disposition": "skip",
        "columns": {
            "version": add_missing_hints({
                "name": "version",
                "data_type": "bigint",
                "nullable": False,
            }),
            "engine_version": add_missing_hints({
                "name": "engine_version",
                "data_type": "bigint",
                "nullable": False
            }),
            "inserted_at": add_missing_hints({
                "name": "inserted_at",
                "data_type": "timestamp",
                "nullable": False
            })
        }
    }


def new_table(table_name: str, parent_name: str = None, columns: Sequence[TColumn] = None) -> TTable:
    table: TTable = {
        "name": table_name,
        "columns": {} if columns is None else {c["name"]: c for c in columns}
    }
    if parent_name:
        table["parent"] = parent_name
    else:
        # set write disposition only for root tables
        table["write_disposition"] = "append"
    return table


def load_table() -> TTable:
    return {
        "description": "Created by DLT. Tracks completed loads",
        "write_disposition": "skip",
        "columns": {
            "load_id": add_missing_hints({
                "name": "load_id",
                "data_type": "text",
                "nullable": False
            }),
            "status": add_missing_hints({
                "name": "status",
                "data_type": "bigint",
                "nullable": False
            }),
            "inserted_at": add_missing_hints({
                "name": "inserted_at",
                "data_type": "timestamp",
                "nullable": False
            })
        }
    }


def standard_hints() -> Dict[THintType, List[str]]:
    return None
