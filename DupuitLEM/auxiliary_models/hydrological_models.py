"""
Hydrological models for DupuitLEM.

Models based on the shear stress forumulation:
    HydrologyEventShearStress, HydrologySteadyShearStress
    --------
    Notes:
    Designed for use with ShearStressModel class. Calculate hydrological state
    and fluvial erosion rate.
    Warning: shear stress models here calculate erosion with an explicit finite
    difference scheme that can be unstable and poorly behaved when incised
    channels approach baselevel. Recommended to not use these...
Models based on streampower forumulation:
    HydrologyEventStreamPower, HydrologySteadyStreamPower
    --------
    Notes:
    Designed for use with StreamPowerModel class. Calculate hydrological state
    and update fields that are used by FastscapeEroder in the StreamPowerModel
    to calculate erosion rates.


Author: David Litwin

Date: 19 May 2020
"""

import numpy as np

from landlab.components import (
    FlowDirectorD8,
    FlowDirectorSteepest,
    FlowAccumulator,
    LakeMapperBarnes,
    DepressionFinderAndRouter,
)


class HydrologicalModel:
    """
    Base class for hydrological model.

    Parameters
    -----
    grid: a landlab grid with GroundwaterDupuitPercolator already instantiated
    routing_method: Either 'D8' or 'Steepest'. This is the routing method for the
        FlowDirector component. 'Steepest' allows the use of non-raster grids.

    """

    def __init__(self, grid, routing_method):

        self._grid = grid

        if routing_method == "D8":
            self.fd = FlowDirectorD8(self._grid)
        elif routing_method == "Steepest":
            self.fd = FlowDirectorSteepest(self._grid)
        else:
            raise ValueError("routing_method must be either D8 or Steepest.")

        self.fa = FlowAccumulator(
            self._grid,
            surface="topographic__elevation",
            flow_director=self.fd,
            runoff_rate="average_surface_water__specific_discharge",
        )
        self.lmb = LakeMapperBarnes(
            self._grid,
            method=routing_method,
            fill_flat=False,
            surface="topographic__elevation",
            fill_surface="topographic__elevation",
            redirect_flow_steepest_descent=False,
            reaccumulate_flow=False,
            track_lakes=False,
            ignore_overfill=True,
        )
        self.dfr = DepressionFinderAndRouter(self._grid)

    def run_step(self):
        raise NotImplementedError


class HydrologyIntegrateShearStress(HydrologicalModel):

    """"
    Stochastic hydrological model running pairs of events and interevents over
    total time specifiied by instantiated PrecipitationDistribution.
    A trapezoidal integration method is used to find effective
    shear stress and erosion rate.

    Note: This method may overestimate shear stress and erosion rate during the
    recession period.

    Parameters
    -----
    grid: landlab grid
    precip_generator: instantiated PrecipitationDistribution
    groundwater_model: instantiated GroundwaterDupuitPercolator
    shear_stress_function: function that takes a grid with topography and
        discharge and returns shear stress at node erosion_rate_function: function
        that takes grid with topography and shear stress and returns erosion rate
    tauc: shear stress threshold for erosion

    """

    def __init__(
        self,
        grid,
        routing_method="D8",
        precip_generator=None,
        groundwater_model=None,
        shear_stress_function=None,
        erosion_rate_function=None,
        tauc=0,
    ):
        super().__init__(grid, routing_method)

        self._tau = self._grid.add_zeros("node", "surface_water__shear_stress")
        self.pd = precip_generator
        self.gdp = groundwater_model
        self.calc_shear_stress = shear_stress_function
        self.calc_erosion_from_shear_stress = erosion_rate_function
        self._tauc = tauc
        self.T_h = self.pd._run_time

    @staticmethod
    def calc_storm_eff_shear_stress(tau0, tau1, tau2, tauc, tr, tb):
        """
        Calculate effective shear stress over the course of an event-interevent
        period using a trapezoidal approximation, which accounts for the linear
        interpolation of when the threshold shear stress is exceeded. Note that this
        method may overestimate shear stress during interevent if it quickly drops
        below the threshold value.
        """

        tauint1 = np.zeros_like(tau0)
        tauint2 = np.zeros_like(tau0)

        c1 = np.logical_and(tau0 > tauc, tau1 > tauc)
        c2 = np.logical_and(tau0 < tauc, tau1 > tauc)
        c3 = np.logical_and(tau0 > tauc, tau1 < tauc)
        # c4 = np.logical_and(tau0<tauc,tau1<tauc) #implied
        tauint1[c1] = 0.5 * tr * (tau0[c1] + tau1[c1] - 2 * tauc)
        tauint1[c2] = (
            0.5 * tr * (tau1[c2] - tauc) * ((tau1[c2] - tauc) / (tau1[c2] - tau0[c2]))
        )
        tauint1[c3] = (
            0.5 * tr * (tau0[c3] - tauc) * ((tau0[c3] - tauc) / (tau0[c3] - tau1[c3]))
        )
        # tauint1[c4] = 0.0 #implied

        c1 = np.logical_and(tau1 > tauc, tau2 > tauc)
        c2 = np.logical_and(tau1 < tauc, tau2 > tauc)
        c3 = np.logical_and(tau1 > tauc, tau2 < tauc)
        # c4 = np.logical_and(tau1<tauc,tau2<tauc) #implied
        tauint2[c1] = 0.5 * tb * (tau1[c1] + tau2[c1] - 2 * tauc)
        tauint2[c2] = (
            0.5 * tb * (tau2[c2] - tauc) * ((tau2[c2] - tauc) / (tau2[c2] - tau1[c2]))
        )
        tauint2[c3] = (
            0.5 * tb * (tau1[c3] - tauc) * ((tau1[c3] - tauc) / (tau1[c3] - tau2[c3]))
        )
        # tauint2[c4] = 0.0 #implied

        taueff = (tauint1 + tauint2) / (tr + tb) + tauc
        return taueff

    def generate_exp_precip(self):
        """
        Generate series of storm_dts, interstorm_dts, and intensities from
        PrecipitationDistribution.
        """
        storm_dts = []
        interstorm_dts = []
        intensities = []

        for (storm_dt, interstorm_dt) in self.pd.yield_storms():
            storm_dts.append(storm_dt)
            interstorm_dts.append(interstorm_dt)
            intensities.append(float(self._grid.at_grid["rainfall__flux"]))

        self.storm_dts = storm_dts
        self.interstorm_dts = interstorm_dts
        self.intensities = intensities

    def run_step(self):

        """
        Hydrological model for series of event-interevent pairs, calculate shear
        stresses at end of event and interevent, calculate erosion rate.
        """

        # generate new precip time series
        self.generate_exp_precip()

        # find and route flow if there are pits
        self.dfr._find_pits()
        if self.dfr._number_of_pits > 0:
            self.lmb.run_one_step()

        # update flow directions
        self.fd.run_one_step()

        self.dzdt = np.zeros_like(self._tau)
        tau2 = self._tau.copy()
        for i in range(len(self.storm_dts)):
            tau0 = tau2.copy()  # save prev end of interstorm shear stress

            # run event, accumulate flow, and calculate resulting shear stress
            self.gdp.recharge = self.intensities[i]
            self.gdp.run_with_adaptive_time_step_solver(self.storm_dts[i])
            _, _ = self.fa.accumulate_flow(update_flow_director=False)
            tau1 = self.calc_shear_stress(self._grid)

            # run interevent, accumulate flow, and calculate resulting shear stress
            self.gdp.recharge = 0.0
            self.gdp.run_with_adaptive_time_step_solver(
                max(self.interstorm_dts[i], 1e-15)
            )
            _, _ = self.fa.accumulate_flow(update_flow_director=False)
            tau2 = self.calc_shear_stress(self._grid)

            # calculate effective shear stress across event-interevent pair
            self._tau[:] = self.calc_storm_eff_shear_stress(
                tau0, tau1, tau2, self._tauc, self.storm_dts[i], self.interstorm_dts[i]
            )

            # calculate erosion rate, and then add time-weighted erosion rate to get effective erosion rate at the end of for loop
            dzdt = self.calc_erosion_from_shear_stress(self._grid)
            self.dzdt += (self.storm_dts[i] + self.interstorm_dts[i]) / self.T_h * dzdt


class HydrologyEventShearStress(HydrologicalModel):

    """"
    Run hydrological model for series of event-interevent pairs, calculate
    instantaneous shear stress and erosion rate at beginning and end of event.
    Calculate average erosion rate *for event only* and average this over the
    whole duration. This method assumes erosion is negligible during the
    interevent periods.

    Parameters
    -----
    grid: landlab grid
    precip_generator: instantiated PrecipitationDistribution
    groundwater_model: instantiated GroundwaterDupuitPercolator
    shear_stress_function: function that takes a grid with topography and
        discharge and returns shear stress at node erosion_rate_function:
        function that takes grid with topography and shear stress and returns
        erosion rate

    """

    def __init__(
        self,
        grid,
        routing_method="D8",
        precip_generator=None,
        groundwater_model=None,
        shear_stress_function=None,
        erosion_rate_function=None,
    ):

        super().__init__(grid, routing_method)

        self._tau = self._grid.add_zeros("node", "surface_water__shear_stress")
        self.pd = precip_generator
        self.gdp = groundwater_model
        self.calc_shear_stress = shear_stress_function
        self.calc_erosion_from_shear_stress = erosion_rate_function
        self.T_h = self.pd._run_time

    def generate_exp_precip(self):

        storm_dts = []
        interstorm_dts = []
        intensities = []

        for (storm_dt, interstorm_dt) in self.pd.yield_storms():
            storm_dts.append(storm_dt)
            interstorm_dts.append(interstorm_dt)
            intensities.append(float(self._grid.at_grid["rainfall__flux"]))

        self.storm_dts = storm_dts
        self.interstorm_dts = interstorm_dts
        self.intensities = intensities

    def run_step(self):
        """"
        Run hydrological model for series of event-interevent pairs, calculate
        shear stresses and calculate effective erosion rate over the
        total_hydrological_time. Erosion rate is from event period only.
        """

        # generate new precip time series
        self.generate_exp_precip()

        # find and route flow if there are pits
        self.dfr._find_pits()
        if self.dfr._number_of_pits > 0:
            self.lmb.run_one_step()

        # update flow directions
        self.fd.run_one_step()

        self.max_substeps_storm = 0
        self.max_substeps_interstorm = 0
        self.dzdt = np.zeros_like(self._tau)
        dzdt2 = np.zeros_like(self._tau)
        for i in range(len(self.storm_dts)):
            dzdt0 = dzdt2.copy()  # save prev end of interstorm erosion rate

            # run event, accumulate flow, and calculate resulting shear stress
            self.gdp.recharge = self.intensities[i]
            self.gdp.run_with_adaptive_time_step_solver(self.storm_dts[i])
            _, _ = self.fa.accumulate_flow(update_flow_director=False)
            self._tau[:] = self.calc_shear_stress(self._grid)
            dzdt1 = self.calc_erosion_from_shear_stress(self._grid)
            self.max_substeps_storm = max(
                self.max_substeps_storm, self.gdp.number_of_substeps
            )

            # run interevent, accumulate flow, and calculate resulting shear stress
            self.gdp.recharge = 0.0
            self.gdp.run_with_adaptive_time_step_solver(
                max(self.interstorm_dts[i], 1e-15)
            )
            _, _ = self.fa.accumulate_flow(update_flow_director=False)
            self._tau[:] = self.calc_shear_stress(self._grid)
            dzdt2 = self.calc_erosion_from_shear_stress(self._grid)
            self.max_substeps_interstorm = max(
                self.max_substeps_interstorm, self.gdp.number_of_substeps
            )

            # calculate erosion, and then add time-weighted erosion rate to get effective erosion rate at the end of for loop
            # note that this only accounts for erosion during the storm period
            deltaz = 0.5 * (dzdt0 + dzdt1) * self.storm_dts[i]
            self.dzdt += deltaz / self.T_h

    def run_step_record_state(self):
        """"
        Run hydrological model for series of event-interevent pairs, calculate
        shear stresses and calculate effective erosion rate over the
        total_hydrological_time

        track the state of the model:
            time: (s)
            intensity: rainfall intensity (m/s)
            wtrel_all: relative water table position (-)
            qs_all: surface water specific discharge (m/s)
            Q_all: discharge (m3/s)
            tau_all: shear stress (N/m2)
        """

        # fields to record:
        self.time = np.zeros(2 * len(self.storm_dts) + 1)
        self.intensity = np.zeros(2 * len(self.storm_dts) + 1)
        self.tau_all = np.zeros(
            (2 * len(self.storm_dts) + 1, len(self._tau))
        )  # all shear stress
        self.Q_all = np.zeros(
            (2 * len(self.storm_dts) + 1, len(self._tau))
        )  # all discharge
        self.wtrel_all = np.zeros(
            (2 * len(self.storm_dts) + 1, len(self._tau))
        )  # all relative water table elevation
        self.qs_all = np.zeros(
            (2 * len(self.storm_dts) + 1, len(self._tau))
        )  # all surface water specific discharge

        self.max_substeps_storm = 0
        self.max_substeps_interstorm = 0

        # find and route flow if there are pits
        self.dfr._find_pits()
        if self.dfr._number_of_pits > 0:
            self.lmb.run_one_step()

        # update flow directions
        self.fd.run_one_step()

        self.max_substeps_storm = 0
        self.max_substeps_interstorm = 0
        self.dzdt = np.zeros_like(self._tau)
        dzdt2 = np.zeros_like(self._tau)
        for i in range(len(self.storm_dts)):
            dzdt0 = dzdt2.copy()  # save prev end of interstorm erosion rate

            # run event, accumulate flow, and calculate resulting shear stress
            self.gdp.recharge = self.intensities[i]
            self.gdp.run_with_adaptive_time_step_solver(self.storm_dts[i])
            _, _ = self.fa.accumulate_flow(update_flow_director=False)
            self._tau[:] = self.calc_shear_stress(self._grid)
            dzdt1 = self.calc_erosion_from_shear_stress(self._grid)
            self.max_substeps_storm = max(
                self.max_substeps_storm, self.gdp.number_of_substeps
            )

            # record event
            self.max_substeps_storm = max(
                self.max_substeps_storm, self.gdp.number_of_substeps
            )
            self.time[i * 2 + 1] = self.time[i * 2] + self.storm_dts[i]
            self.intensity[i * 2] = self.intensities[i]
            self.tau_all[i * 2 + 1, :] = self._tau
            self.Q_all[i * 2 + 1, :] = self._grid.at_node["surface_water__discharge"]
            self.wtrel_all[i * 2 + 1, :] = (self._wt - self._base) / (
                self._elev - self._base
            )
            self.qs_all[i * 2 + 1, :] = self._grid.at_node[
                "surface_water__specific_discharge"
            ]

            # run interevent, accumulate flow, and calculate resulting shear stress
            self.gdp.recharge = 0.0
            self.gdp.run_with_adaptive_time_step_solver(self.interstorm_dts[i])
            _, _ = self.fa.accumulate_flow(update_flow_director=False)
            self._tau[:] = self.calc_shear_stress(self._grid)
            dzdt2 = self.calc_erosion_from_shear_stress(self._grid)
            self.max_substeps_interstorm = max(
                self.max_substeps_interstorm, self.gdp.number_of_substeps
            )

            # record interevent
            self.max_substeps_interstorm = max(
                self.max_substeps_interstorm, self.gdp.number_of_substeps
            )
            self.time[i * 2 + 2] = self.time[i * 2 + 1] + self.interstorm_dts[i]
            self.tau_all[i * 2 + 2, :] = self._tau
            self.Q_all[i * 2 + 2, :] = self._grid.at_node["surface_water__discharge"]
            self.wtrel_all[i * 2 + 2, :] = (self._wt - self._base) / (
                self._elev - self._base
            )
            self.qs_all[i * 2 + 2, :] = self._grid.at_node[
                "surface_water__specific_discharge"
            ]

            # calculate erosion, and then add time-weighted erosion rate to get effective erosion rate at the end of for loop
            # note that this only accounts for erosion during the storm period
            deltaz = 0.5 * (dzdt0 + dzdt1) * self.storm_dts[i]
            self.dzdt += deltaz / self.T_h


class HydrologySteadyShearStress(HydrologicalModel):
    """"
    Run hydrological model for steady recharge.
    Calculate shear stress and erosion rate at the end of the timestep

    Parameters
    -----
    grid: landlab grid
    groundwater_model: instantiated GroundwaterDupuitPercolator
    shear_stress_function: function that takes a grid with topography and
        discharge and returns shear stress at node
    erosion_rate_function: function that takes grid with topography and shear
        stress and returns erosion rate

    """

    def __init__(
        self,
        grid,
        routing_method="D8",
        groundwater_model=None,
        shear_stress_function=None,
        erosion_rate_function=None,
        hydrological_timestep=1e5,
    ):
        super().__init__(grid, routing_method)

        self._tau = self._grid.add_zeros("node", "surface_water__shear_stress")
        self.gdp = groundwater_model
        self.calc_shear_stress = shear_stress_function
        self.calc_erosion_from_shear_stress = erosion_rate_function
        self.T_h = hydrological_timestep

    def run_step(self):
        """
        Run steady model one step. Update groundwater state, route and accumulate flow,
        calculate shear stress and erosion rate.
        """

        # run gw model
        self.gdp.run_with_adaptive_time_step_solver(self.T_h)
        self.number_substeps = self.gdp.number_of_substeps

        # find pits for flow accumulation
        self.dfr._find_pits()
        if self.dfr._number_of_pits > 0:
            self.lmb.run_one_step()

        # run flow accumulation
        self.fa.run_one_step()

        # calc shear stress and erosion
        self._tau[:] = self.calc_shear_stress(self._grid)
        self.dzdt = self.calc_erosion_from_shear_stress(self._grid)


class HydrologySteadyStreamPower(HydrologicalModel):
    """"
    Run hydrological model for steady recharge provided to the
    GroundwaterDupuitPercolator. HydrologySteadyStreamPower is meant to be
    passed to the StreamPowerModel, where erosion rate is calculated.
    An additional field, surface_water_area_norm__discharge is calculated
    by dividing the effective discharge by the square root of the drainage area.
    This accounts for how channel width varies with the square root of area.
    When combined with FastscapeEroder with m=1 and n=1, this produces erosion
    with the form E = K v0 Q* sqrt(a) S, where Q*=Q/(pA).

    Parameters
    -----
    grid: landlab grid
    precip_generator: instantiated PrecipitationDistribution
    groundwater_model: instantiated GroundwaterDupuitPercolator
    """

    def __init__(
        self,
        grid,
        routing_method="D8",
        groundwater_model=None,
        hydrological_timestep=1e5,
    ):
        super().__init__(grid, routing_method)

        self.gdp = groundwater_model
        self.T_h = hydrological_timestep

        self.q_an = self._grid.add_zeros("node", "surface_water_area_norm__discharge")
        self.area = self._grid.at_node["drainage_area"]
        self.q = self._grid.at_node["surface_water__discharge"]

    def run_step(self):
        """
        Run steady model one step. Update groundwater state, route and
        accumulate flow, updating surface_water__discharge.
        """

        # run gw model
        self.gdp.run_with_adaptive_time_step_solver(self.T_h)
        self.number_substeps = self.gdp.number_of_substeps

        # find pits for flow accumulation
        self.dfr._find_pits()
        if self.dfr._number_of_pits > 0:
            self.lmb.run_one_step()

        # run flow accumulation on average_surface_water__specific_discharge
        self.fa.run_one_step()

        # add a criteria that effectively cuts Q* off at 1 (greater due to numerical issues)
        qmax = self.gdp.recharge * self.area
        self.q[self.q > qmax] = qmax[self.q > qmax]

        # discharge field with form for Q*
        self.q_an[:] = np.divide(
            self.q, np.sqrt(self.area), where=self.area > 0, out=np.zeros_like(self.q)
        )


class HydrologyEventStreamPower(HydrologicalModel):

    """"
    Run hydrological model for series of event-interevent pairs, calculate
    instantaneous flow rate at the beginning and end of event. This method
    assumes erosion is negligible during the interevent periods.
    HydrologyEventStreamPower is meant to be passed to
    StochasticRechargeStreamPower, where erosion rate is calculated.
    An additional field, surface_water_area_norm__discharge is calculated
    by dividing the effective discharge by the square root of the drainage area.
    This accounts for how channel width varies with the square root of area.
    When combined with FastscapeEroder with m=1 and n=1, this produces erosion
    with the form E = K v0 Q* sqrt(a) S, where Q*=Q/(pA).

    Parameters
    -----
    grid: landlab grid
    precip_generator: instantiated PrecipitationDistribution
    groundwater_model: instantiated GroundwaterDupuitPercolator

    """

    def __init__(
        self, grid, routing_method="D8", precip_generator=None, groundwater_model=None,
    ):

        super().__init__(grid, routing_method)

        self.q_eff = self._grid.add_zeros("node", "surface_water_effective__discharge")
        self.q_an = self._grid.add_zeros("node", "surface_water_area_norm__discharge")
        self.area = self._grid.at_node["drainage_area"]
        self.pd = precip_generator
        self.gdp = groundwater_model
        self.T_h = self.pd._run_time

    def generate_exp_precip(self):
        """
        Generate all storms ahead of time. Used with run_step_record_state.
        """

        storm_dts = []
        interstorm_dts = []
        intensities = []

        for (storm_dt, interstorm_dt) in self.pd.yield_storms():
            storm_dts.append(storm_dt)
            interstorm_dts.append(interstorm_dt)
            intensities.append(float(self._grid.at_grid["rainfall__flux"]))

        self.storm_dts = storm_dts
        self.interstorm_dts = interstorm_dts
        self.intensities = intensities

    def run_step(self):
        """"
        Run hydrological model for series of event-interevent pairs, calculate
        flow rates at end of events and interevents over total_hydrological_time.
        Effective flow rates are calculated during event periods only.
        Update groundwater state, routes and accumulates flow, update
        surface_water_effective__discharge and surface_water_area_norm__discharge.
        """

        # find and route flow if there are pits
        self.dfr._find_pits()
        if self.dfr._number_of_pits > 0:
            self.lmb.run_one_step()

        # update flow directions
        self.fd.run_one_step()

        self.max_substeps_storm = 0
        self.max_substeps_interstorm = 0
        q_total_vol = np.zeros_like(self.q_eff)
        q2 = np.zeros_like(self.q_eff)
        for (storm_dt, interstorm_dt) in self.pd.yield_storms():

            intensity = float(self._grid.at_grid["rainfall__flux"])

            q0 = q2.copy()  # save prev end of interstorm flow rate

            # run event, accumulate flow
            self.gdp.recharge = intensity
            self.gdp.run_with_adaptive_time_step_solver(storm_dt)
            _, q = self.fa.accumulate_flow(update_flow_director=False)
            q1 = q.copy()
            self.max_substeps_storm = max(
                self.max_substeps_storm, self.gdp.number_of_substeps
            )

            # run interevent, accumulate flow
            self.gdp.recharge = 0.0
            self.gdp.run_with_adaptive_time_step_solver(max(interstorm_dt, 1e-15))
            _, q = self.fa.accumulate_flow(update_flow_director=False)
            q2 = q.copy()
            self.max_substeps_interstorm = max(
                self.max_substeps_interstorm, self.gdp.number_of_substeps
            )

            # volume of runoff contributed during timestep
            q_total_vol += 0.5 * (q0 + q1) * storm_dt

        self.q_eff[:] = q_total_vol / self.T_h
        self.q_an[:] = np.divide(
            self.q_eff,
            np.sqrt(self.area),
            where=self.area > 0,
            out=np.zeros_like(self.q_eff),
        )

    def run_step_record_state(self):
        """"
        Run hydrological model for series of event-interevent pairs, calculate
        flow rates at end of events and interevents over total_hydrological_time.
        Effective flow rates are calculated during event periods only.
        Update groundwater state, routes and accumulates flow, update
        surface_water_effective__discharge and surface_water_area_norm__discharge.

        track the state of the model:
            time: (s)
            intensity: rainfall intensity (m/s)
            wtrel_all: relative water table position (-)
            qs_all: surface water specific discharge (m/s)
            Q_all: discharge (m3/s)

        """

        # generate new precip time series
        self.generate_exp_precip()

        # find and route flow if there are pits
        self.dfr._find_pits()
        if self.dfr._number_of_pits > 0:
            self.lmb.run_one_step()

        # update flow directions
        self.fd.run_one_step()

        # fields to record:
        Ns = 2 * len(self.storm_dts) + 1
        N = len(self.q_eff)
        self.time = np.zeros(Ns)
        self.intensity = np.zeros(Ns)
        # all discharge
        self.Q_all = np.zeros((Ns, N))
        # water table elevation
        self.wt_all = np.zeros((Ns, N))
        self.wt_all[0, :] = self._grid.at_node["water_table__elevation"].copy()
        # all surface water specific discharge
        self.qs_all = np.zeros((Ns, N))

        self.max_substeps_storm = 0
        self.max_substeps_interstorm = 0

        q_total_vol = np.zeros_like(self.q_eff)
        q2 = np.zeros_like(self.q_eff)
        for i in range(len(self.storm_dts)):
            q0 = q2.copy()  # save prev end of interstorm flow rate

            # run event, accumulate flow
            self.gdp.recharge = self.intensities[i]
            self.gdp.run_with_adaptive_time_step_solver(self.storm_dts[i])
            _, q = self.fa.accumulate_flow(update_flow_director=False)
            q1 = q.copy()
            self.max_substeps_storm = max(
                self.max_substeps_storm, self.gdp.number_of_substeps
            )

            # record event
            self.time[i * 2 + 1] = self.time[i * 2] + self.storm_dts[i]
            self.intensity[i * 2] = self.intensities[i]
            self.Q_all[i * 2 + 1, :] = self._grid.at_node["surface_water__discharge"]
            self.wt_all[i * 2 + 1, :] = self._grid.at_node["water_table__elevation"]
            self.qs_all[i * 2 + 1, :] = self._grid.at_node[
                "average_surface_water__specific_discharge"
            ]

            # run interevent, accumulate flow
            self.gdp.recharge = 0.0
            self.gdp.run_with_adaptive_time_step_solver(
                max(self.interstorm_dts[i], 1e-15)
            )
            _, q = self.fa.accumulate_flow(update_flow_director=False)
            q2 = q.copy()
            self.max_substeps_interstorm = max(
                self.max_substeps_interstorm, self.gdp.number_of_substeps
            )

            # record interevent
            self.time[i * 2 + 2] = self.time[i * 2 + 1] + self.interstorm_dts[i]
            self.Q_all[i * 2 + 2, :] = self._grid.at_node["surface_water__discharge"]
            self.wt_all[i * 2 + 2, :] = self._grid.at_node["water_table__elevation"]
            self.qs_all[i * 2 + 2, :] = self._grid.at_node[
                "average_surface_water__specific_discharge"
            ]

            # volume of runoff contributed during timestep
            q_total_vol += 0.5 * (q0 + q1) * self.storm_dts[i]

        self.q_eff[:] = q_total_vol / self.T_h
        self.q_an[:] = np.divide(
            self.q_eff,
            np.sqrt(self.area),
            where=self.area > 0,
            out=np.zeros_like(self.q_eff),
        )


class HydrologyEventThresholdStreamPower(HydrologyEventStreamPower):
    """
    This model expands the HydrologyEventStreamPower model for
    cases where there is a streampower incision threshold. Rather than tracking
    discharge at storms and interstorms, erosion-producing discharge Q-Q0 is
    tracked, and returned in a fashion that is correctly averaged with threshold
    accounted for in the field "surface_water_effective__discharge".
    CONSEQUENTLY ONE SHOULD LEAVE THE INCISION THRESHOLD FIELD OF
    FastscapeEroder SET TO ZERO! For the same reason, this field has a different
    meaning when using the threshold model: it is not an actual discharge,
    but an effective value that also accounts for geomorphic properties related
    to the threshold streampower.

    Parameters
    -----
    grid: landlab grid
    routing_method: either "D8" or "Steepest"
    precip_generator: instantiated PrecipitationDistribution
    groundwater_model: instantiated GroundwaterDupuitPercolator
    E0: the streampower incision threshold in the equation
        E = K v0 Q* sqrt(a) S - E0, where Q*=Q/(pA). Units: L/T
        Default value: 0.0
    sp_coefficient: streampower coefficient used in the FastscapeEroder
        component. Note that in DupuitLEM, the expected value has units 1/L,
        because it takes a coefficient K/p, where K is the usual streampower
        coefficient, with units 1/T, and p is the mean precipitation rate with
        units L/T.
        Default value: 1e-12
    """

    def __init__(
        self,
        grid,
        routing_method="D8",
        precip_generator=None,
        groundwater_model=None,
        sp_threshold=0.0,
        sp_coefficient=1e-5,
    ):
        super().__init__(grid, routing_method, precip_generator, groundwater_model)
        self.E0 = sp_threshold
        self.Ksp = sp_coefficient
        self.Q0 = self._grid.add_zeros("node", "critical_erosion__discharge")
        self._elev = self._grid.at_node["topographic__elevation"]
        self._recievers = self._grid.at_node["flow__link_to_receiver_node"]

        if routing_method == "D8":
            self._calc_grad = self._grid.calc_grad_at_d8
        else:
            self._calc_grad = self._grid.calc_grad_at_link

    def run_step(self):
        """"
        Run hydrological model for series of event-interevent pairs, calculate
        flow rates at end of events and interevents over total_hydrological_time.
        Effective flow rates are calculated during event periods only.
        Update groundwater state, routes and accumulates flow, update
        surface_water_effective__discharge and surface_water_area_norm__discharge.
        """

        # find and route flow if there are pits
        self.dfr._find_pits()
        if self.dfr._number_of_pits > 0:
            self.lmb.run_one_step()

        # update flow directions
        self.fd.run_one_step()

        # calculate critical_erosion__discharge
        dzdx = self._calc_grad(self._elev)
        S = abs(dzdx[self._recievers])  # slope is in direction of flow
        _, _ = self.fa.accumulate_flow(update_flow_director=False)  # to update area
        self.Q0[:] = np.divide(
            self.E0 * np.sqrt(self.area),
            self.Ksp * S,
            where=S > 0.0,
            out=np.zeros_like(S),
        )

        self.max_substeps_storm = 0
        self.max_substeps_interstorm = 0
        q_total_vol = np.zeros_like(self.q_eff)
        q2 = np.zeros_like(self.q_eff)
        for (storm_dt, interstorm_dt) in self.pd.yield_storms():

            intensity = float(self._grid.at_grid["rainfall__flux"])

            q0 = q2.copy()  # save prev end of interstorm flow rate

            # run event, accumulate flow
            self.gdp.recharge = intensity
            self.gdp.run_with_adaptive_time_step_solver(storm_dt)
            _, q = self.fa.accumulate_flow(update_flow_director=False)
            q1 = np.maximum(q - self.Q0, 0.0)
            # print('storm q:%f, q1:%f'%(q[4], q1[4]))
            self.max_substeps_storm = max(
                self.max_substeps_storm, self.gdp.number_of_substeps
            )

            # run interevent, accumulate flow
            self.gdp.recharge = 0.0
            self.gdp.run_with_adaptive_time_step_solver(max(interstorm_dt, 1e-15))
            _, q = self.fa.accumulate_flow(update_flow_director=False)
            q2 = np.maximum(q - self.Q0, 0.0)
            # print('interstorm q:%f, q2:%f'%(q[4], q2[4]))
            self.max_substeps_interstorm = max(
                self.max_substeps_interstorm, self.gdp.number_of_substeps
            )

            # volume of runoff contributed during timestep
            q_total_vol += 0.5 * (q0 + q1) * storm_dt
            # print('storm dt:%f'%storm_dt)

        self.q_eff[:] = q_total_vol / self.T_h
        self.q_an[:] = np.divide(
            self.q_eff,
            np.sqrt(self.area),
            where=self.area > 0,
            out=np.zeros_like(self.q_eff),
        )

    def run_step_record_state(self):
        """"
        Run hydrological model for series of event-interevent pairs, calculate
        flow rates at end of events and interevents over total_hydrological_time.
        Effective flow rates are calculated during event periods only.
        Update groundwater state, routes and accumulates flow, update
        surface_water_effective__discharge and surface_water_area_norm__discharge.

        track the state of the model:
            time: (s)
            intensity: rainfall intensity (m/s)
            wtrel_all: relative water table position (-)
            qs_all: surface water specific discharge (m/s)
            Q_all: discharge (m3/s)

        """

        # generate new precip time series
        self.generate_exp_precip()

        # find and route flow if there are pits
        self.dfr._find_pits()
        if self.dfr._number_of_pits > 0:
            self.lmb.run_one_step()

        # update flow directions
        self.fd.run_one_step()

        # calculate critical_erosion__discharge
        dzdx = self._calc_grad(self._elev)
        S = abs(dzdx[self._recievers])  # slope is in direction of flow
        _, _ = self.fa.accumulate_flow(update_flow_director=False)  # to update area
        self.Q0[:] = np.divide(
            self.E0 * np.sqrt(self.area),
            self.Ksp * S,
            where=S > 0.0,
            out=np.zeros_like(S),
        )

        # fields to record:
        Ns = 2 * len(self.storm_dts) + 1
        N = len(self.q_eff)
        self.time = np.zeros(Ns)
        self.intensity = np.zeros(Ns)
        # all discharge
        self.Q_all = np.zeros((Ns, N))
        # water table elevation
        self.wt_all = np.zeros((Ns, N))
        self.wt_all[0, :] = self._grid.at_node["water_table__elevation"].copy()
        # all surface water specific discharge
        self.qs_all = np.zeros((Ns, N))

        self.max_substeps_storm = 0
        self.max_substeps_interstorm = 0

        q_total_vol = np.zeros_like(self.q_eff)
        q2 = np.zeros_like(self.q_eff)
        for i in range(len(self.storm_dts)):
            q0 = q2.copy()  # save prev end of interstorm flow rate

            # run event, accumulate flow
            self.gdp.recharge = self.intensities[i]
            self.gdp.run_with_adaptive_time_step_solver(self.storm_dts[i])
            _, q = self.fa.accumulate_flow(update_flow_director=False)
            q1 = np.maximum(q - self.Q0, 0.0)
            # print('storm q:%f, q1:%f'%(q[4], q1[4]))
            self.max_substeps_storm = max(
                self.max_substeps_storm, self.gdp.number_of_substeps
            )

            # record event
            self.time[i * 2 + 1] = self.time[i * 2] + self.storm_dts[i]
            self.intensity[i * 2] = self.intensities[i]
            self.Q_all[i * 2 + 1, :] = self._grid.at_node["surface_water__discharge"]
            self.wt_all[i * 2 + 1, :] = self._grid.at_node["water_table__elevation"]
            self.qs_all[i * 2 + 1, :] = self._grid.at_node[
                "average_surface_water__specific_discharge"
            ]

            # run interevent, accumulate flow
            self.gdp.recharge = 0.0
            self.gdp.run_with_adaptive_time_step_solver(
                max(self.interstorm_dts[i], 1e-15)
            )
            _, q = self.fa.accumulate_flow(update_flow_director=False)
            q2 = np.maximum(q - self.Q0, 0.0)
            # print('interstorm q:%f, q2:%f'%(q[4], q2[4]))
            self.max_substeps_interstorm = max(
                self.max_substeps_interstorm, self.gdp.number_of_substeps
            )

            # record interevent
            self.time[i * 2 + 2] = self.time[i * 2 + 1] + self.interstorm_dts[i]
            self.Q_all[i * 2 + 2, :] = self._grid.at_node["surface_water__discharge"]
            self.wt_all[i * 2 + 2, :] = self._grid.at_node["water_table__elevation"]
            self.qs_all[i * 2 + 2, :] = self._grid.at_node[
                "average_surface_water__specific_discharge"
            ]

            # volume of runoff contributed during timestep
            q_total_vol += 0.5 * (q0 + q1) * self.storm_dts[i]

        self.q_eff[:] = q_total_vol / self.T_h
        self.q_an[:] = np.divide(
            self.q_eff,
            np.sqrt(self.area),
            where=self.area > 0,
            out=np.zeros_like(self.q_eff),
        )


class HydrologyEventVadoseStreamPower(HydrologyEventStreamPower):
    def __init__(
        self,
        grid,
        routing_method="D8",
        precip_generator=None,
        groundwater_model=None,
        vadose_model=None,
    ):
        super().__init__(grid, routing_method, precip_generator, groundwater_model)

        self.svm = vadose_model
        self.r = self._grid.add_zeros("node", "recharge_rate")
        self.elev = self._grid.at_node["topographic__elevation"]
        self.wt = self._grid.at_node["water_table__elevation"]

    def run_step(self):
        """"
        Run hydrological model for series of event-interevent pairs, calculate
        flow rates at end of events and interevents over total_hydrological_time.
        Effective flow rates are calculated during event periods only.
        Update groundwater state, routes and accumulates flow, update
        surface_water_effective__discharge and surface_water_area_norm__discharge.
        """
        cores = self._grid.core_nodes

        # find and route flow if there are pits
        self.dfr._find_pits()
        if self.dfr._number_of_pits > 0:
            self.lmb.run_one_step()

        # update flow directions
        self.fd.run_one_step()

        self.max_substeps_storm = 0
        self.max_substeps_interstorm = 0
        q_total_vol = np.zeros_like(self.q_eff)
        q2 = np.zeros_like(self.q_eff)
        for (storm_dt, interstorm_dt) in self.pd.yield_storms():

            intensity = float(self._grid.at_grid["rainfall__flux"])
            q0 = q2.copy()  # save prev end of interstorm flow rate

            # run event:
            ## run vadose model, calculate recharge based on depth to wt
            self.svm.run_event(intensity * storm_dt)
            wt_from_surface = self.elev[cores] - self.wt[cores]
            wt_digitized = np.digitize(wt_from_surface, self.svm.depths, right=True)
            wt_digitized[wt_digitized == len(self.svm.depths)] = (
                len(self.svm.depths) - 1
            )
            self.r[cores] = self.svm.recharge_at_depth[wt_digitized] / storm_dt

            ## set recharge, run groundwater model, accumulate flow
            self.gdp.recharge = self.r
            self.gdp.run_with_adaptive_time_step_solver(storm_dt)
            _, q = self.fa.accumulate_flow(update_flow_director=False)
            q1 = q.copy()
            self.max_substeps_storm = max(
                self.max_substeps_storm, self.gdp.number_of_substeps
            )

            # run interevent:
            ## run vadose model, set recharge, run groundwater model, accumulate flow
            self.svm.run_interevent(interstorm_dt)
            self.gdp.recharge = 0.0
            self.gdp.run_with_adaptive_time_step_solver(max(interstorm_dt, 1e-15))
            _, q = self.fa.accumulate_flow(update_flow_director=False)
            q2 = q.copy()
            self.max_substeps_interstorm = max(
                self.max_substeps_interstorm, self.gdp.number_of_substeps
            )

            # volume of runoff contributed during timestep
            q_total_vol += 0.5 * (q0 + q1) * storm_dt

        # set effective runoff rates
        self.q_eff[:] = q_total_vol / self.T_h
        self.q_an[:] = np.divide(
            self.q_eff,
            np.sqrt(self.area),
            where=self.area > 0,
            out=np.zeros_like(self.q_eff),
        )

    def run_step_record_state(self):
        """"
        Run hydrological model for series of event-interevent pairs, calculate
        flow rates at end of events and interevents over total_hydrological_time.
        Effective flow rates are calculated during event periods only.
        Update groundwater state, routes and accumulates flow, update
        surface_water_effective__discharge and surface_water_area_norm__discharge.

        track the state of the model:
            time: (s)
            intensity: rainfall intensity (m/s)
            wtrel_all: relative water table position (-)
            qs_all: surface water specific discharge (m/s)
            Q_all: discharge (m3/s)

        """
        cores = self._grid.core_nodes

        # generate new precip time series
        self.generate_exp_precip()

        # find and route flow if there are pits
        self.dfr._find_pits()
        if self.dfr._number_of_pits > 0:
            self.lmb.run_one_step()

        # update flow directions
        self.fd.run_one_step()

        # fields to record:
        Ns = 2 * len(self.storm_dts) + 1
        N = len(self.q_eff)
        self.time = np.zeros(Ns)
        self.intensity = np.zeros(Ns)
        # all discharge
        self.Q_all = np.zeros((Ns, N))
        # water table elevation
        self.wt_all = np.zeros((Ns, N))
        self.wt_all[0, :] = self._grid.at_node["water_table__elevation"].copy()
        # all surface water specific discharge
        self.qs_all = np.zeros((Ns, N))
        # all recharge
        self.r_all = np.zeros((Ns, N))

        # vadose profile properties
        self.cum_recharge_profile = np.zeros_like(self.svm.depths)
        self.bool_recharge_profile = np.zeros_like(self.svm.depths)

        # precip/recharge spatially-averaged properties
        areas = self._grid.cell_area_at_node[cores]
        obn = self._grid.open_boundary_nodes
        area_tot = np.sum(areas)
        self.cum_precip = 0.0
        self.cum_recharge = 0.0
        self.cum_exfiltration = 0.0

        self.max_substeps_storm = 0
        self.max_substeps_interstorm = 0

        q_total_vol = np.zeros_like(self.q_eff)
        q2 = np.zeros_like(self.q_eff)
        for i in range(len(self.storm_dts)):
            q0 = q2.copy()  # save prev end of interstorm flow rate

            # run event:
            ## run vadose model, calculate recharge based on depth to wt
            self.svm.run_event(self.intensities[i] * self.storm_dts[i])
            wt_from_surface = self.elev[cores] - self.wt[cores]
            wt_digitized = np.digitize(wt_from_surface, self.svm.depths, right=True)
            wt_digitized[wt_digitized == len(self.svm.depths)] = (
                len(self.svm.depths) - 1
            )
            self.r[cores] = self.svm.recharge_at_depth[wt_digitized] / self.storm_dts[i]

            ## set recharge, run groundwater model, accumulate flow
            self.gdp.recharge = self.r
            self.gdp.run_with_adaptive_time_step_solver(self.storm_dts[i])
            _, q = self.fa.accumulate_flow(update_flow_director=False)
            q1 = q.copy()
            self.max_substeps_storm = max(
                self.max_substeps_storm, self.gdp.number_of_substeps
            )

            # record event
            self.time[i * 2 + 1] = self.time[i * 2] + self.storm_dts[i]
            self.intensity[i * 2] = self.intensities[i]
            self.r_all[i * 2, :] = self._grid.at_node["recharge_rate"]
            self.Q_all[i * 2 + 1, :] = self._grid.at_node["surface_water__discharge"]
            self.wt_all[i * 2 + 1, :] = self._grid.at_node["water_table__elevation"]
            self.qs_all[i * 2 + 1, :] = self._grid.at_node[
                "average_surface_water__specific_discharge"
            ]

            # run interevent:
            ## run vadose model, set recharge, run groundwater model, accumulate flow
            self.svm.run_interevent(self.interstorm_dts[i])
            self.gdp.recharge = 0.0
            self.gdp.run_with_adaptive_time_step_solver(
                max(self.interstorm_dts[i], 1e-15)
            )
            _, q = self.fa.accumulate_flow(update_flow_director=False)
            q2 = q.copy()
            self.max_substeps_interstorm = max(
                self.max_substeps_interstorm, self.gdp.number_of_substeps
            )

            # record interevent
            self.time[i * 2 + 2] = self.time[i * 2 + 1] + self.interstorm_dts[i]
            self.Q_all[i * 2 + 2, :] = self._grid.at_node["surface_water__discharge"]
            self.wt_all[i * 2 + 2, :] = self._grid.at_node["water_table__elevation"]
            self.qs_all[i * 2 + 2, :] = self._grid.at_node[
                "average_surface_water__specific_discharge"
            ]

            # record vadose characteristics
            self.cum_recharge_profile += self.svm.recharge_at_depth
            self.bool_recharge_profile += self.svm.recharge_at_depth > 0.0

            # record precip/recharge spatially-averaged characteristics
            self.cum_precip += np.sum(self.intensities[i] * areas) * self.storm_dts[i]
            self.cum_recharge += np.sum(self.r[cores] * areas) * self.storm_dts[i]
            self.cum_exfiltration += np.sum(q1[obn] * self.storm_dts[i] + q2[obn] * self.interstorm_dts[i])

            # volume of runoff contributed during timestep
            q_total_vol += 0.5 * (q0 + q1) * self.storm_dts[i]

        self.q_eff[:] = q_total_vol / self.T_h
        self.q_an[:] = np.divide(
            self.q_eff,
            np.sqrt(self.area),
            where=self.area > 0,
            out=np.zeros_like(self.q_eff),
        )

        # derived properties
        # mean recharge at each vadose profile
        self.mean_recharge_depth = (
            self.cum_recharge_profile / self.bool_recharge_profile
        )
        # frequency of recharge in vadose profile
        self.recharge_frequency = self.bool_recharge_profile / self.T_h
