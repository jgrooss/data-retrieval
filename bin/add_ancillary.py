"""
Copyright (C) 2021 by Forschungszentrum Juelich GmbH
Author(s): Joern Ungermann, May Baer
"""
import datetime
import itertools
import optparse
import os
import sys
from metpy.calc import potential_temperature, potential_vorticity_baroclinic, brunt_vaisala_frequency_squared, geopotential_to_height
from metpy.units import units
import xarray as xr

import netCDF4
import numpy as np
import tqdm

VARIABLES = {
    "pressure": ("FULL", "hPa", "air_pressure", "Pressure"),
    "pt": ("FULL", "K", "air_potential_temperature", "Potential Temperature"),
    "pv": ("FULL", "m^2 K s^-1 kg^-1 10E-6", "ertel_potential_vorticity", "Potential Vorticity"),
    "mod_pv": ("FULL", "m^2 K s^-1 kg^-1 10E-6", "", "Modified Potential Vorticity"),
    "zh": ("FULL", "km", "geopotential_height", "Geopotential Altitude"),
    "n2": ("FULL", "s^-2", "square_of_brunt_vaisala_frequency_in_air", "N^2"),
    "TROPOPAUSE": ("HORIZONTAL", "km", "tropopause_altitude",
                   "vertical location of first WMO thermal tropopause"),
    "TROPOPAUSE_PRESSURE": ("HORIZONTAL", "Pa", "tropopause_air_pressure",
                            "vertical location of first WMO thermal tropopause"),
    "TROPOPAUSE_THETA": ("HORIZONTAL", "K", "tropopause_air_potential_temperature",
                         "vertical location of first WMO thermal tropopause"),
    "TROPOPAUSE_SECOND": ("HORIZONTAL", "km", "secondary_tropopause_altitude",
                          "vertical location of second WMO thermal tropopause"),
    "TROPOPAUSE_SECOND_PRESSURE": ("HORIZONTAL", "Pa", "secondary_tropopause_air_pressure",
                                   "vertical location of second WMO thermal tropopause"),
    "TROPOPAUSE_SECOND_THETA": ("HORIZONTAL", "K", "secondary_tropopause_air_potential_temperature",
                                "vertical location of second WMO thermal tropopause"),
}


def get_create_variable(ncin, name):
    """
    Either retrieves a variable from NetCDF or creates it,
    in case it is not yet present.
    """
    if name not in ncin.variables:
        if name in VARIABLES:
            dim, units, standard_name, long_name = VARIABLES[name]
        var_id = ncin.createVariable(name, "f4", ("time", "lev_2", "lat", "lon"),
                                     **{"zlib": 1, "shuffle": 1, "fletcher32": 1, "fill_value": np.nan})
        var_id.units = units
        var_id.long_name = long_name
        if standard_name:
            var_id.standard_name = standard_name
    return ncin.variables[name]


def find_tropopause(alts, temps):
    """
    Identifies position of thermal tropopauses in given altitude/temperature
    profile. Has some issues with inversions, which is circumventyed partly by
    setting seek to False, which is not strictly necessary by WMO definition.

    The thermal definition of the tropopause, WMO, 1957:

    (a) The first tropopause is defined as the lowest level at which the lapse
    rate decreases to 2 degree C/km or less, provided also the average lapse rate
    between this level and all higher levels within 2 km does not exceed 2 degree C/km.

    (b) If above the first tropopause the average lapse rate between any level
    and all higher levels within 1 km exceeds 3 degree C/km, then a second tropopause
    is defined by the same criterion as under (a). This tropopause may be either
    within or above the 1 km layer.
    """
    dtdz_wmo = -2
    zmin = 5
    zmax = 22
    alts = np.asarray(alts)
    temps = np.asarray(temps)
    valid = (~(np.isnan(alts) | np.isnan(temps))) & (alts > 2.0) & (alts < 30.0)
    alts, temps = alts[valid], temps[valid]
    if len(alts) < 3:
        return []
    if alts[0] > alts[1]:  # check for proper order and reverse if necessary
        alts = alts[::-1]
        temps = temps[::-1]

    result = []
    # This differentiation is sufficient as we are looking at average lapse rate
    # with respect to higher levels anyway, so using a more accurate left/right
    # differentiation does not really improve things here.
    lapse_rate = (temps[1:] - temps[:-1]) / (alts[1:] - alts[:-1])
    lapse_alts = (alts[1:] + alts[:-1]) / 2.
    seek = True
    for j in range(1, len(lapse_rate)):
        if not seek and lapse_rate[j] < -3:
            ks = [k for k in range(len(temps)) if lapse_alts[j] <= alts[k] <= lapse_alts[j] + 1.]
            # This way of calculating the average lapse rate is optimal. Don't
            # try to improve. Integrate t'/(z1-z0) numerically (no trapez! do it
            # stupid way) with infinitesimal h. Differentiate numerically using
            # same h. Simplify. Voila. As h can be assumed as small as possible,
            # this is accurate.
            if len(ks) > 1:
                k, ks = ks[0], ks[1:]
                avg_lapse = (temps[ks] - temps[k]) / (alts[ks] - alts[k])
                if all(avg_lapse < -3):
                    seek = True
            else:
                seek = True

        if seek and lapse_rate[j - 1] <= dtdz_wmo < lapse_rate[j] \
                and zmin < lapse_alts[j] < zmax:
            alt = np.interp([dtdz_wmo],
                            lapse_rate[j - 1:j + 1], lapse_alts[j - 1:j + 1])[0]

            ks = [_k for _k in range(len(temps)) if alt <= alts[_k] <= alt + 2.]
            if len(ks) > 1:
                k, ks = ks[0], ks[1:]
                avg_lapse = (temps[ks] - temps[k]) / (alts[ks] - alts[k])
                if all(avg_lapse > dtdz_wmo):
                    result.append(alt)
                    seek = False
            else:
                result.append(alt)
                seek = False
    return result


def parse_args(args):
    oppa = optparse.OptionParser(usage="""
    add_ancillary.py

    Adds PV and ancillary quantities to 4D model data given as NetCDF.

    Usage: add_pv.py [options] <netCDF file>

    Example:
    add_pv.py ecmwfr_ana_ml_06072912.nc
    """)

    oppa.add_option('--theta', '', action='store_true',
                    help="Add pt potential temperature field")
    oppa.add_option('--n2', '', action='store_true',
                    help="Add n2 static stability.")
    oppa.add_option('--pv', '', action='store_true',
                    help="Add pv potential vorticity.")
    oppa.add_option('--tropopause', '', action='store_true',
                    help="Add first and second tropopause")
    opt, arg = oppa.parse_args(args)

    if len(arg) != 1:
        print(oppa.get_usage())
        exit(1)
    if not os.path.exists(arg[0]):
        print("Cannot find model data at", arg[1])
        exit(1)
    return opt, arg[0]


def add_tropopauses(ncin):
    """
    Adds first and second thermal WMO tropopause to model. Fill value is -999.
    """
    print("Adding first and second tropopause")

    temp = (units(ncin.variables["t"].units) *
            ncin.variables["t"][:]).to("K").m
    press = (units(ncin.variables["pressure"].units) *
             ncin.variables["pressure"][:]).to("hPa").m
    gph = geopotential_to_height(
        units(ncin.variables["zh"].units) * ncin.variables["zh"][:]).to("km").m
    theta = (units(ncin.variables["pt"].units) *
             ncin.variables["pt"][:]).to("K").m

    if gph[0, 1, 0, 0] < gph[0, 0, 0, 0]:
        gph = gph[:, ::-1, :, :]
        press = press[:, ::-1, :, :]
        temp = temp[:, ::-1, :, :]
        theta = theta[:, ::-1, :, :]

    valid = np.isfinite(gph[0, :, 0, 0])
    assert gph[0, valid, 0, 0][1] > gph[0, valid, 0, 0][0]
    assert press[0, valid, 0, 0][1] < press[0, valid, 0, 0][0]

    above_tropo1 = np.empty((gph.shape[0], gph.shape[2], gph.shape[3]))
    above_tropo1[:] = np.nan
    above_tropo2 = above_tropo1.copy()
    above_tropo1_press = above_tropo1.copy()
    above_tropo2_press = above_tropo1.copy()
    above_tropo1_theta = above_tropo1.copy()
    above_tropo2_theta = above_tropo1.copy()
    for iti, ilo, ila in tqdm.tqdm(
            itertools.product(range(gph.shape[0]), range(gph.shape[3]), range(gph.shape[2])),
            total=gph.shape[0] * gph.shape[3] * gph.shape[2], ascii=True):
        tropopauses = find_tropopause(gph[iti, :, ila, ilo], temp[iti, :, ila, ilo])
        tropopauses = [x for x in tropopauses if 5 < x < 22]
        if len(tropopauses) > 0:
            above_tropo1[iti, ila, ilo] = min(tropopauses)
            above_tropo1_press[iti, ila, ilo] = np.interp(
                above_tropo1[iti, ila, ilo], gph[iti, :, ila, ilo], press[iti, :, ila, ilo])
            above_tropo1_theta[iti, ila, ilo] = np.interp(
                above_tropo1[iti, ila, ilo], gph[iti, :, ila, ilo], theta[iti, :, ila, ilo])
            second = [x for x in tropopauses if x > above_tropo1[iti, ila, ilo]]
            if len(second) > 0:
                above_tropo2[iti, ila, ilo] = min(second)
            above_tropo2_press[iti, ila, ilo] = np.interp(
                above_tropo2[iti, ila, ilo], gph[iti, :, ila, ilo], press[iti, :, ila, ilo])
            above_tropo2_theta[iti, ila, ilo] = np.interp(
                above_tropo2[iti, ila, ilo], gph[iti, :, ila, ilo], theta[iti, :, ila, ilo])

    above_tropo1_press = np.exp(above_tropo1_press)
    above_tropo2_press = np.exp(above_tropo2_press)

    get_create_variable(ncin, "TROPOPAUSE")[:] = above_tropo1
    get_create_variable(ncin, "TROPOPAUSE_SECOND")[:] = above_tropo2
    get_create_variable(ncin, "TROPOPAUSE_PRESSURE")[:] = above_tropo1_press * 100
    get_create_variable(ncin, "TROPOPAUSE_SECOND_PRESSURE")[:] = above_tropo2_press * 100
    get_create_variable(ncin, "TROPOPAUSE_THETA")[:] = above_tropo1_theta
    get_create_variable(ncin, "TROPOPAUSE_SECOND_THETA")[:] = above_tropo2_theta


def add_metpy(option, filename):
    """
    Adds the variables possible through metpy (theta, pv, n2)
    """
    with xr.load_dataset(filename) as xin:
        if option.theta or option.pv:
            print("Adding potential temperature...")
            xin["pt"] = potential_temperature(xin["pressure"], xin["t"])
            xin["pt"].data = np.array(xin["pt"].data)
            xin["pt"].attrs["units"] = "K"
            xin["pt"].attrs["standard_name"] = VARIABLES["pt"][2]
        if option.pv:
            print("Adding potential vorticity...")
            xin = xin.metpy.assign_crs(grid_mapping_name='latitude_longitude',
                                       earth_radius=6.356766e6)
            xin["pv"] = potential_vorticity_baroclinic(xin["pt"], xin["pressure"], xin["u"], xin["v"])
            xin["pv"].data = np.array(xin["pv"].data * 10 ** 6)
            xin = xin.drop("metpy_crs")
            xin["pv"].attrs["units"] = VARIABLES["pv"][1]
            xin["pv"].attrs["standard_name"] = VARIABLES["pv"][2]
            xin["mod_pv"] = xin["pv"] * ((xin["pt"] / 360) ** (-4.5))
            xin["mod_pv"].attrs["standard_name"] = VARIABLES["mod_pv"][2]
            xin["mod_pv"].attrs["units"] = VARIABLES["mod_pv"][1]
        if option.n2:
            print("Adding N2...")
            xin["n2"] = brunt_vaisala_frequency_squared(geopotential_to_height(xin["zh"]), xin["pt"])
            xin["n2"].data = np.array(xin["n2"].data)
            xin["n2"].attrs["units"] = VARIABLES["n2"][1]
            xin["n2"].attrs["standard_name"] = "square_of_brunt_vaisala_frequency_in_air"

        xin.to_netcdf(filename)


def add_rest(option, filename):
    """
    Adds the variables not possible through metpy
    """
    # Open NetCDF file as passed from command line
    with netCDF4.Dataset(filename, "r+") as ncin:

        history = datetime.datetime.now().isoformat() + ":" + " ".join(sys.argv)
        if hasattr(ncin, "history"):
            history += "\n" + ncin.history
        ncin.history = history
        ncin.date_modified = datetime.datetime.now().isoformat()

        if option.tropopause:
            add_tropopauses(ncin)


def main():
    option, filename = parse_args(sys.argv[1:])
    add_metpy(option, filename)
    add_rest(option, filename)


if __name__ == "__main__":
    main()
