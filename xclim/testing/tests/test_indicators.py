#!/usr/bin/env python
# Tests for the Indicator objects
import gc
import json
from inspect import signature
from typing import Optional, Tuple, Union

import dask
import numpy as np
import pytest
import xarray as xr

import xclim
from xclim import __version__, atmos
from xclim.core.calendar import select_time
from xclim.core.formatting import (
    AttrFormatter,
    default_formatter,
    merge_attributes,
    parse_doc,
    update_history,
)
from xclim.core.indicator import Daily, Indicator, ResamplingIndicator, registry
from xclim.core.units import convert_units_to, declare_units, units
from xclim.core.utils import InputKind, MissingVariableError
from xclim.indices import tg_mean
from xclim.testing import open_dataset


@declare_units(da="[temperature]", thresh="[temperature]")
def uniindtemp_compute(da: xr.DataArray, thresh: str = "0.0 degC", freq: str = "YS"):
    """Docstring"""
    out = da - convert_units_to(thresh, da)
    out = out.resample(time=freq).mean()
    out.attrs["units"] = da.units
    return out


uniIndTemp = Daily(
    realm="atmos",
    identifier="tmin",
    module="test",
    cf_attrs=[
        dict(
            var_name="tmin{thresh.magnitude:g}",
            units="K",
            long_name="{freq} mean surface temperature",
            standard_name="{freq} mean temperature",
            cell_methods="time: mean within {freq:noun}",
            another_attr="With a value.",
        )
    ],
    compute=uniindtemp_compute,
)


@declare_units(da="[precipitation]")
def uniindpr_compute(da: xr.DataArray, freq: str):
    """Docstring"""
    return da.resample(time=freq).mean(keep_attrs=True)


uniIndPr = Daily(
    realm="atmos",
    identifier="prmax",
    cf_attrs=[dict(units="mm/s")],
    context="hydro",
    module="test",
    compute=uniindpr_compute,
)


@declare_units(da="[temperature]")
def uniclim_compute(da: xr.DataArray, freq="YS", **indexer):
    select = select_time(da, **indexer)
    return select.mean(dim="time", keep_attrs=True).expand_dims("time")


uniClim = ResamplingIndicator(
    src_freq="D",
    realm="atmos",
    identifier="clim",
    cf_attrs=[dict(units="K")],
    module="test",
    compute=uniclim_compute,
)


@declare_units(tas="[temperature]")
def multitemp_compute(tas: xr.DataArray, freq: str):
    return (
        tas.resample(time=freq).min(keep_attrs=True),
        tas.resample(time=freq).max(keep_attrs=True),
    )


multiTemp = Daily(
    realm="atmos",
    identifier="minmaxtemp",
    cf_attrs=[
        dict(
            var_name="tmin",
            units="K",
            standard_name="Min temp",
            description="Grouped computation of tmax and tmin",
        ),
        dict(
            var_name="tmax",
            units="K",
            description="Grouped computation of tmax and tmin",
        ),
    ],
    module="test",
    compute=multitemp_compute,
)


@declare_units(tas="[temperature]", tasmin="[temperature]", tasmax="[temperature]")
def multioptvar_compute(
    tas: Optional[xr.DataArray] = None,
    tasmax: Optional[xr.DataArray] = None,
    tasmin: Optional[xr.DataArray] = None,
):
    if tas is None:
        with xr.set_options(keep_attrs=True):
            return (tasmin + tasmax) / 2
    return tas


multiOptVar = Indicator(
    src_freq="D",
    realm="atmos",
    identifier="multiopt",
    cf_attrs=[dict(units="K")],
    module="test",
    compute=multioptvar_compute,
)


def test_attrs(tas_series):
    import datetime as dt

    a = tas_series(np.arange(360.0))
    txm = uniIndTemp(a, thresh="5 degC", freq="YS")
    assert txm.cell_methods == "time: mean within days time: mean within years"
    assert f"{dt.datetime.now():%Y-%m-%d %H}" in txm.attrs["history"]
    assert "TMIN(da=tas, thresh='5 degC', freq='YS')" in txm.attrs["history"]
    assert f"xclim version: {__version__}" in txm.attrs["history"]
    assert txm.name == "tmin5"
    assert uniIndTemp.standard_name == "{freq} mean temperature"
    assert uniIndTemp.cf_attrs[0]["another_attr"] == "With a value."


def test_opt_vars(tasmin_series, tasmax_series):
    tn = tasmin_series(np.zeros(365))
    tx = tasmax_series(np.zeros(365))

    multiOptVar(tasmin=tn, tasmax=tx)
    assert multiOptVar.parameters["tasmin"]["kind"] == InputKind.OPTIONAL_VARIABLE


def test_registering():
    assert "test.TMIN" in registry

    # Because this has not been instantiated, it's not in any registry.
    class Test123(registry["test.TMIN"]):
        identifier = "test123"

    assert "test.TEST123" not in registry
    Test123(module="test")
    assert "test.TEST123" in registry

    # Confirm registries live in subclasses.
    class IndicatorNew(Indicator):
        pass

    # Identifier must be given
    with pytest.raises(AttributeError, match="has not been set."):
        IndicatorNew()

    # Realm must be given
    with pytest.raises(AttributeError, match="realm must be given"):
        IndicatorNew(identifier="i2d")

    indnew = IndicatorNew(identifier="i2d", realm="atmos", module="test")
    assert "test.I2D" in registry
    assert registry["test.I2D"].get_instance() is indnew

    del indnew
    gc.collect()
    with pytest.raises(ValueError, match="There is no existing instance"):
        registry["test.I2D"].get_instance()


def test_module():
    """Translations are keyed according to the module where the indicators are defined."""
    assert atmos.tg_mean.__module__.split(".")[2] == "atmos"
    # Virtual module also are stored under xclim.indicators
    assert xclim.indicators.cf.fg.__module__ == "xclim.indicators.cf"
    assert xclim.indicators.icclim.GD4.__module__ == "xclim.indicators.icclim"


def test_temp_unit_conversion(tas_series):
    a = tas_series(np.arange(365), start="2001-01-01")
    txk = uniIndTemp(a, freq="YS")

    # This is not supposed to work
    uniIndTemp.units = "degC"
    txc = uniIndTemp(a, freq="YS")
    with pytest.raises(AssertionError):
        np.testing.assert_array_almost_equal(txk, txc + 273.15)

    uniIndTemp.cf_attrs[0]["units"] = "degC"
    txc = uniIndTemp(a, freq="YS")
    np.testing.assert_array_almost_equal(txk, txc + 273.15)


def test_multiindicator(tas_series):
    tas = tas_series(np.arange(366), start="2000-01-01")
    tmin, tmax = multiTemp(tas, freq="YS")

    assert tmin[0] == tas.min()
    assert tmax[0] == tas.max()
    assert tmin.attrs["standard_name"] == "Min temp"
    assert tmin.attrs["description"] == "Grouped computation of tmax and tmin"
    assert tmax.attrs["description"] == "Grouped computation of tmax and tmin"
    assert multiTemp.units == ["K", "K"]

    # Attrs passed as keywords - together
    ind = Daily(
        realm="atmos",
        identifier="minmaxtemp2",
        cf_attrs=[
            dict(
                var_name="tmin",
                units="K",
                standard_name="Min temp",
                description="Grouped computation of tmax and tmin",
            ),
            dict(
                var_name="tmax",
                units="K",
                description="Grouped computation of tmax and tmin",
            ),
        ],
        compute=multitemp_compute,
    )
    tmin, tmax = ind(tas, freq="YS")
    assert tmin[0] == tas.min()
    assert tmax[0] == tas.max()
    assert tmin.attrs["standard_name"] == "Min temp"
    assert tmin.attrs["description"] == "Grouped computation of tmax and tmin"
    assert tmax.attrs["description"] == "Grouped computation of tmax and tmin"

    with pytest.raises(ValueError, match="Output #2 is missing a var_name!"):
        ind = Daily(
            realm="atmos",
            identifier="minmaxtemp2",
            cf_attrs=[
                dict(
                    var_name="tmin",
                    units="K",
                ),
                dict(
                    units="K",
                ),
            ],
            compute=multitemp_compute,
        )

    # Attrs passed as keywords - individually
    ind = Daily(
        realm="atmos",
        identifier="minmaxtemp3",
        var_name=["tmin", "tmax"],
        units="K",
        standard_name=["Min temp", ""],
        description="Grouped computation of tmax and tmin",
        compute=multitemp_compute,
    )
    tmin, tmax = ind(tas, freq="YS")
    assert tmin[0] == tas.min()
    assert tmax[0] == tas.max()
    assert tmin.attrs["standard_name"] == "Min temp"
    assert tmin.attrs["description"] == "Grouped computation of tmax and tmin"
    assert tmax.attrs["description"] == "Grouped computation of tmax and tmin"
    assert ind.units == ["K", "K"]

    # All must be the same length
    with pytest.raises(ValueError, match="Attribute var_name has 2 elements"):
        ind = Daily(
            realm="atmos",
            identifier="minmaxtemp3",
            var_name=["tmin", "tmax"],
            units="K",
            standard_name=["Min temp"],
            description="Grouped computation of tmax and tmin",
            compute=uniindpr_compute,
        )

    ind = Daily(
        realm="atmos",
        identifier="minmaxtemp4",
        var_name=["tmin", "tmax"],
        units="K",
        standard_name=["Min temp", ""],
        description="Grouped computation of tmax and tmin",
        compute=uniindtemp_compute,
    )
    with pytest.raises(ValueError, match="Indicator minmaxtemp4 was wrongly defined"):
        tmin, tmax = ind(tas, freq="YS")


def test_missing(tas_series):
    a = tas_series(np.ones(365, float), start="1/1/2000")

    # By default, missing is set to "from_context", and the default missing option is "any"
    # Cannot set missing_options with "from_context"
    with pytest.raises(ValueError, match="Cannot set `missing_options`"):
        uniClim.__class__(missing_options={"tolerance": 0.01})

    # Null value
    a[5] = np.nan

    m = uniIndTemp(a, freq="MS")
    assert m[0].isnull()

    with xclim.set_options(
        check_missing="pct", missing_options={"pct": {"tolerance": 0.05}}
    ):
        m = uniIndTemp(a, freq="MS")
        assert not m[0].isnull()

    with xclim.set_options(check_missing="wmo"):
        m = uniIndTemp(a, freq="YS")
        assert m[0].isnull()

    # With freq=None
    c = uniClim(a)
    assert c.isnull()

    # With indexer
    ci = uniClim(a, month=[2])
    assert not ci.isnull()

    out = uniClim(a, month=[1])
    assert out.isnull()


def test_missing_from_context(tas_series):
    a = tas_series(np.ones(365, float), start="1/1/2000")
    # Null value
    a[5] = np.nan

    ind = uniIndTemp.__class__(missing="from_context")

    m = ind(a, freq="MS")
    assert m[0].isnull()


def test_json(pr_series):
    meta = uniIndPr.json()

    expected = {
        "identifier",
        "title",
        "keywords",
        "abstract",
        "parameters",
        "history",
        "references",
        "notes",
        "outputs",
    }

    output_exp = {
        "var_name",
        "units",
        "long_name",
        "standard_name",
        "cell_methods",
        "description",
        "comment",
    }

    assert set(meta.keys()).issubset(expected)
    for output in meta["outputs"]:
        assert set(output.keys()).issubset(output_exp)


def test_all_jsonable(official_indicators):
    problems = []
    for identifier, ind in official_indicators.items():
        indinst = ind.get_instance()
        try:
            json.dumps(indinst.json())
        except (TypeError, KeyError):
            problems.append(identifier)
    if problems:
        raise ValueError(
            f"Indicators {problems} provide problematic json serialization."
        )


def test_all_parameters_understood(official_indicators):
    problems = []
    for identifier, ind in official_indicators.items():
        indinst = ind.get_instance()
        for name, param in indinst.parameters.items():
            if param["kind"] == InputKind.OTHER_PARAMETER:
                problems.append((identifier, name))
    if problems:
        raise ValueError(
            f"The following indicator/parameter couple {problems} use types not listed in InputKind."
        )


def test_signature():
    sig = signature(xclim.atmos.solid_precip_accumulation)
    assert list(sig.parameters.keys()) == ["pr", "tas", "thresh", "freq", "ds"]
    assert sig.parameters["pr"].annotation == Union[xr.DataArray, str]
    assert sig.parameters["tas"].default == "tas"
    assert sig.parameters["tas"].kind == sig.parameters["tas"].POSITIONAL_OR_KEYWORD
    assert sig.parameters["thresh"].kind == sig.parameters["thresh"].KEYWORD_ONLY
    assert sig.return_annotation == xr.DataArray

    sig = signature(xclim.atmos.wind_speed_from_vector)
    assert sig.return_annotation == Tuple[xr.DataArray, xr.DataArray]


def test_doc():
    doc = xclim.atmos.fire_weather_indexes.__doc__
    assert doc.startswith("Fire weather indexes. (realm: atmos)")
    assert "This indicator will check for missing values according to the method" in doc
    assert "Based on indice :py:func:`~xclim.indices.fwi.fire_weather_indexes`." in doc
    assert "ffmc0 : str or DataArray, optional" in doc
    assert "Returns\n-------" in doc
    assert "See https://cwfis.cfs.nrcan.gc.ca/background/dsm/fwi, the module's" in doc
    assert "Updated source code for calculating fire danger indexes in the" in doc


def test_delayed(tasmax_series):
    tasmax = tasmax_series(np.arange(360.0)).chunk({"time": 5})
    out = uniIndTemp(tasmax)
    assert isinstance(out.data, dask.array.Array)


def test_identifier():
    with pytest.warns(UserWarning):
        uniIndPr.__class__(identifier="t_{}")


def test_formatting(pr_series):
    out = atmos.wetdays(pr_series(np.arange(366)), thresh=1.0 * units.mm / units.day)
    # pint 0.10 now pretty print day as d.
    assert out.attrs["long_name"] in [
        "Number of wet days (precip >= 1 mm d-1)",
        "Number of wet days (precip >= 1 mm d-1)",
    ]
    out = atmos.wetdays(pr_series(np.arange(366)), thresh=1.5 * units.mm / units.day)
    assert out.attrs["long_name"] in [
        "Number of wet days (precip >= 1.5 mm d-1)",
        "Number of wet days (precip >= 1.5 mm d-1)",
    ]


def test_parse_doc():
    doc = parse_doc(tg_mean.__doc__)
    assert doc["title"] == "Mean of daily average temperature."
    assert (
        doc["abstract"]
        == "Resample the original daily mean temperature series by taking the mean over each period."
    )
    assert doc["parameters"]["tas"]["description"] == "Mean daily temperature."
    assert doc["parameters"]["freq"]["description"] == "Resampling frequency."
    assert doc["notes"].startswith("Let")
    assert "math::" in doc["notes"]
    assert "references" not in doc
    assert doc["long_name"] == "The mean daily temperature at the given time frequency"

    doc = parse_doc(xclim.indices.saturation_vapor_pressure.__doc__)
    assert (
        doc["parameters"]["ice_thresh"]["description"]
        == "Threshold temperature under which to switch to equations in reference to ice instead of water. If None (default) everything is computed with reference to water."
    )
    assert "Goff, J. A., and S. Gratch (1946)" in doc["references"]


def test_parsed_doc():
    assert "tas" in xclim.atmos.liquid_precip_accumulation.parameters

    params = xclim.atmos.drought_code.parameters
    assert params["tas"]["description"] == "Noon temperature."
    assert params["tas"]["units"] == "[temperature]"
    assert params["tas"]["kind"] is InputKind.VARIABLE
    assert params["tas"]["default"] == "tas"
    assert params["snd"]["default"] is None
    assert params["snd"]["kind"] is InputKind.OPTIONAL_VARIABLE
    assert params["snd"]["units"] == "[length]"
    assert params["season_method"]["kind"] is InputKind.STRING
    assert params["season_method"]["choices"] == {"GFWED", None, "WF93", "LA08"}


def test_default_formatter():
    assert default_formatter.format("{freq}", freq="YS") == "annual"
    assert default_formatter.format("{freq:noun}", freq="MS") == "months"
    assert default_formatter.format("{month}", month="m3") == "march"


def test_AttrFormatter():
    fmt = AttrFormatter(
        mapping={"evil": ["méchant", "méchante"], "nice": ["beau", "belle"]},
        modifiers=["m", "f"],
    )
    # Normal cases
    assert fmt.format("{adj:m}", adj="evil") == "méchant"
    assert fmt.format("{adj:f}", adj="nice") == "belle"
    # Missing mod:
    assert fmt.format("{adj}", adj="evil") == "méchant"
    # Mod with unknown value
    with pytest.raises(ValueError):
        fmt.format("{adj:m}", adj="funny")


@pytest.mark.parametrize("new_line", ["<>", "\n"])
@pytest.mark.parametrize("missing_str", ["<Missing>", None])
def test_merge_attributes(missing_str, new_line):
    a = xr.DataArray([0], attrs={"text": "Text1"}, name="a")
    b = xr.DataArray([0], attrs={})
    c = xr.Dataset(attrs={"text": "Text3"})

    merged = merge_attributes(
        "text", a, missing_str=missing_str, new_line=new_line, b=b, c=c
    )

    assert merged.startswith("a: Text1")

    if missing_str is not None:
        assert merged.count(new_line) == 2
        assert f"b: {missing_str}" in merged
    else:
        assert merged.count(new_line) == 1
        assert "b:" not in merged


def test_update_history():
    a = xr.DataArray([0], attrs={"history": "Text1"}, name="a")
    b = xr.DataArray([0], attrs={"history": "Text2"})
    c = xr.Dataset(attrs={"history": "Text3"})

    merged = update_history("text", a, new_name="d", b=b, c=c)

    assert "d: text" in merged.split("\n")[-1]
    assert merged.startswith("a: Text1")


def test_input_dataset():
    ds = open_dataset("ERA5/daily_surface_cancities_1990-1993.nc")

    # Use defaults
    out = xclim.atmos.daily_temperature_range(freq="YS", ds=ds)

    # Use non-defaults (inverted on purpose)
    with xclim.set_options(cf_compliance="log"):
        out = xclim.atmos.daily_temperature_range("tasmax", "tasmin", freq="YS", ds=ds)

    # Use a mix
    out = xclim.atmos.daily_temperature_range(tasmax=ds.tasmax, freq="YS", ds=ds)

    # Inexistent variable:
    dsx = ds.drop_vars("tasmin")
    with pytest.raises(MissingVariableError):
        out = xclim.atmos.daily_temperature_range(freq="YS", ds=dsx)  # noqa


def test_indicator_from_dict():
    d = dict(
        realm="atmos",
        cf_attrs=dict(
            var_name="tmean{threshold}",
            units="K",
            long_name="{freq} mean surface temperature",
            standard_name="{freq} mean temperature",
            cell_methods=[{"time": "mean within days"}],
        ),
        compute="thresholded_statistics",
        parameters=dict(
            threshold={"description": "A threshold temp"},
            condition="<",
            reducer="mean",
        ),
        input={"data": "tas"},
    )

    ind = Daily.from_dict(d, identifier="tmean", module="test")

    assert ind.realm == "atmos"
    # Parameters metadata modification
    assert ind.parameters["threshold"].description == "A threshold temp"
    # Injection of paramters
    assert ind.injected_parameters["condition"] == "<"
    # Default value for input variable injected and meta injected
    assert ind._variable_mapping["data"] == "tas"
    assert signature(ind).parameters["tas"].default == "tas"
    assert ind.parameters["tas"].units == "K"

    # Wrap a multi-output ind
    d = dict(base="wind_speed_from_vector")
    ind = Indicator.from_dict(d, identifier="wsfv", module="test")


def test_indicator_errors():
    def func(data: xr.DataArray, thresh: str = "0 degC", freq: str = "YS"):
        return data

    doc = [
        "The title",
        "",
        "    The abstract",
        "",
        "    Parameters",
        "    ----------",
        "    data : xr.DataArray",
        "      A variable.",
        "    thresh : str",
        "      A threshold",
        "    freq : str",
        "      The resampling frequency.",
        "",
        "    Returns",
        "    -------",
        "    xr.DataArray, [K]",
        "      An output",
    ]
    func.__doc__ = "\n".join(doc)

    d = dict(
        realm="atmos",
        cf_attrs=dict(
            var_name="tmean{threshold}",
            units="K",
            long_name="{freq} mean surface temperature",
            standard_name="{freq} mean temperature",
            cell_methods=[{"time": "mean within days"}],
        ),
        compute=func,
        input={"data": "tas"},
    )
    ind = Daily(identifier="indi", module="test", **d)

    with pytest.raises(AttributeError, match="`identifier` has not been set"):
        Daily(**d)

    d["identifier"] = "bad_indi"
    d["module"] = "test"

    bad_doc = doc[:12] + ["    extra: bool", "      Woupsi"] + doc[12:]
    func.__doc__ = "\n".join(bad_doc)
    with pytest.raises(ValueError, match="Malformed docstring"):
        Daily(**d)

    func.__doc__ = "\n".join(doc)
    d["parameters"] = {}
    d["parameters"]["thresh"] = "1 degK"
    d["parameters"]["extra"] = "woopsi again"
    with pytest.raises(ValueError, match="Parameter 'extra' was passed but it does"):
        Daily(**d)

    del d["parameters"]["extra"]
    d["input"]["data"] = "3nsd6sk72"
    with pytest.raises(ValueError, match="Compute argument data was mapped to"):
        Daily(**d)

    d2 = dict(input={"tas": "sfcWind"})
    with pytest.raises(ValueError, match="When changing the name of a variable by"):
        ind.__class__(**d2)

    del d["input"]
    with pytest.raises(ValueError, match="variable data is missing expected units"):
        Daily(**d)

    d["parameters"]["thresh"] = {"units": "K"}
    d["realm"] = "mercury"
    d["input"] = {"data": "tasmin"}
    with pytest.raises(AttributeError, match="Indicator's realm must be given as one"):
        Daily(**d)

    def func(data: xr.DataArray, thresh: str = "0 degC"):
        return data

    func.__doc__ = "\n".join(doc[:10] + doc[12:])
    d = dict(
        realm="atmos",
        cf_attrs=dict(
            var_name="tmean{threshold}",
            units="K",
            long_name="{freq} mean surface temperature",
            standard_name="{freq} mean temperature",
            cell_methods=[{"time": "mean within days"}],
        ),
        compute=func,
        input={"data": "tas"},
    )
    with pytest.raises(ValueError, match="ResamplingIndicator require a 'freq'"):
        ind = Daily(identifier="indi", module="test", **d)


def test_indicator_call_errors(tas_series):
    tas = tas_series(np.arange(730), start="2001-01-01")
    uniIndTemp(da=tas, thresh="3 K")

    with pytest.raises(TypeError, match="too many positional arguments"):
        uniIndTemp(tas, tas)

    with pytest.raises(TypeError, match="got an unexpected keyword argument 'oups'"):
        uniIndTemp(tas, oups=3)


def test_resamplingIndicator_new_error():
    with pytest.raises(ValueError, match="ResamplingIndicator require a 'freq'"):
        Daily(
            realm="atmos",
            identifier="multiopt",
            cf_attrs=[dict(units="K")],
            module="test",
            compute=multioptvar_compute,
        )


def test_resampling_indicator_with_indexing(tas_series):
    tas = tas_series(np.ones(731) + 273.15, start="2003-01-01")

    out = xclim.atmos.tx_days_above(tas, thresh="0 degC", freq="YS")
    np.testing.assert_allclose(out, [365, 366])

    out = xclim.atmos.tx_days_above(tas, thresh="0 degC", freq="YS", month=2)
    np.testing.assert_allclose(out, [28, 29])

    out = xclim.atmos.tx_days_above(
        tas, thresh="0 degC", freq="AS-JUL", doy_bounds=(1, 50)
    )
    np.testing.assert_allclose(out, [50, 50, np.NaN])

    out = xclim.atmos.tx_days_above(
        tas, thresh="0 degC", freq="YS", date_bounds=("02-29", "04-01")
    )
    np.testing.assert_allclose(out, [32, 33])
