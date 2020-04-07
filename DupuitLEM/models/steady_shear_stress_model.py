"""
Landscape evolution model using the GroundwaterDupuitPercolator in which
recharge occurs at a steady rate through the model duration. Here fluvial erosion
is calculated by calculating excess shear stress.

Author: David Litwin

"""
import time
import numpy as np

from landlab.components import (
    GroundwaterDupuitPercolator,
    FlowAccumulator,
    LinearDiffuser,
    LakeMapperBarnes,
    DepressionFinderAndRouter,
    )
from landlab.io.netcdf import write_raster_netcdf
from DupuitLEM.grid_functions.grid_funcs import calc_shear_stress_at_node, calc_erosion_from_shear_stress

class SteadyRechargeShearStress:

    """
    Simple groundwater landscape evolution model with constant uplift/baselevel
    fall, linear hillslope diffusive transport, and detachment limited erosion
    generated by accumulated groundwater return flow and saturation excess overland flow
    from steady recharge to a shallow aquifer.

    """

    def __init__(self,params,save_output=True):

        self._grid = params.pop("grid")
        self._cores = self._grid.core_nodes

        self.R = params.pop("recharge_rate") #[m/s]
        self.Ksat = params.pop("hydraulic_conductivity") #[m/s]
        self.n = params.pop("porosity")
        self.r = params.pop("regularization_factor")
        self.c = params.pop("courant_coefficient")
        self.vn = params.pop("vn_coefficient")

        self.w0 = params.pop("permeability_production_rate") #[m/s]
        self.d_s = params.pop("characteristic_w_depth")
        self.U = params.pop("uplift_rate") # uniform uplift [m/s]
        self.b_st = params.pop("b_st") #shear stress erosion exponent
        self.k_st = params.pop("k_st") #shear stress erosion coefficient
        self.Tauc = params.pop("shear_stress_threshold") #threshold shear stress [N/m2]
        self.n_manning = params.pop("manning_n") #manning's n for flow depth calcualtion
        self.D = params.pop("hillslope_diffusivity") # hillslope diffusivity [m2/s]

        self.dt_h = params.pop("hydrological_timestep") # hydrological timestep [s]
        self.T = params.pop("total_time") # total simulation time [s]
        self.MSF = params.pop("morphologic_scaling_factor") # morphologic scaling factor [-]
        self.dt_m = self.MSF*self.dt_h
        self.N = int(self.T//self.dt_m)

        self._elev = self._grid.at_node["topographic__elevation"]
        self._base = self._grid.at_node["aquifer_base__elevation"]
        self._wt = self._grid.at_node["water_table__elevation"]
        self._gw_flux = self._grid.add_zeros('node', 'groundwater__specific_discharge_node')
        self._tau = self._grid.add_zeros('node',"surface_water__shear_stress")

        if save_output:
            self.save_output = True
            self.output_interval = params.pop("output_interval")
            self.output_fields = params.pop("output_fields")
            self.base_path = params.pop("base_output_path")
            self.id =  params.pop("run_id")
        else:
            self.save_output = False

        # initialize model components
        self.gdp = GroundwaterDupuitPercolator(self._grid, porosity=self.n, hydraulic_conductivity=self.Ksat, \
                                          recharge_rate=self.R, regularization_f=self.r, \
                                          courant_coefficient=self.c, vn_coefficient = self.vn)
        self.fa = FlowAccumulator(self._grid, surface='topographic__elevation', flow_director='D8',  \
                              runoff_rate='average_surface_water__specific_discharge')
        self.lmb = LakeMapperBarnes(self._grid, method='D8', fill_flat=False,
                                      surface='topographic__elevation',
                                      fill_surface='topographic__elevation',
                                      redirect_flow_steepest_descent=False,
                                      reaccumulate_flow=False,
                                      track_lakes=False,
                                      ignore_overfill=True)
        self.ld = LinearDiffuser(self._grid, linear_diffusivity = self.D)
        self.dfr = DepressionFinderAndRouter(self._grid)


    def run_model(self):
        """ run the model for the full duration"""

        N = self.N
        num_substeps = np.zeros(N)
        max_rel_change = np.zeros(N)
        perc90_rel_change = np.zeros(N)
        times = np.zeros((N,5))
        num_pits = np.zeros(N)

        # Run model forward
        for i in range(N):
            elev0 = self._elev.copy()

            t1 = time.time()
            #run gw model
            self.gdp.run_with_adaptive_time_step_solver(self.dt_h)
            num_substeps[i] = self.gdp.number_of_substeps

            t2 = time.time()
            #uplift and regolith production
            self._elev[self._cores] += self.U*self.dt_m
            self._base[self._cores] += self.U*self.dt_m - self.w0*np.exp(-(self._elev[self._cores]-self._base[self._cores])/self.d_s)*self.dt_m

            t3 = time.time()
            #find pits for flow accumulation
            self.dfr._find_pits()
            if self.dfr._number_of_pits > 0:
                self.lmb.run_one_step()

            t4 = time.time()
            #run flow accumulation
            self.fa.run_one_step()

            t5 = time.time()
            #run linear diffusion
            self.ld.run_one_step(self.dt_m)

            #calc shear stress and erosion
            self._tau[:] = calc_shear_stress_at_node(self._grid,n_manning = self.n_manning)
            dzdt = calc_erosion_from_shear_stress(self._grid,self.Tauc,self.k_st,self.b_st)
            self._elev += dzdt*self.dt_m

            #check for places where erosion to bedrock occurs
            self._elev[self._elev<self._base] = self._base[self._elev<self._base]

            t6 = time.time()
            times[i:] = [t2-t1, t3-t2, t4-t3, t5-t4, t6-t5]
            num_pits[i] = self.dfr._number_of_pits
            elev_diff = abs(self._elev-elev0)/elev0
            max_rel_change[i] = np.max(elev_diff)
            perc90_rel_change[i] = np.percentile(elev_diff,90)

            if self.save_output:

                if i % self.output_interval == 0 or i==max(range(N)):
                    self._gw_flux[:] = self.gdp.calc_gw_flux_at_node()

                    filename = self.base_path + str(self.id) + '_grid_' + str(i) + '.nc'
                    write_raster_netcdf(filename, self._grid, names = self.output_fields, format="NETCDF4")
                    print('Completed loop %d' % i)

                    filename = self.base_path + str(self.id) + '_substeps' + '.txt'
                    np.savetxt(filename,num_substeps, fmt='%.1f')

                    filename = self.base_path + str(self.id) + '_max_rel_change' + '.txt'
                    np.savetxt(filename,max_rel_change, fmt='%.4e')

                    filename = self.base_path + str(self.id) + '_90perc_rel_change' + '.txt'
                    np.savetxt(filename,perc90_rel_change, fmt='%.4e')

                    filename = self.base_path + str(self.id) + '_num_pits' + '.txt'
                    np.savetxt(filename,num_pits, fmt='%.1f')

                    filename = self.base_path + str(self.id) + '_time' + '.txt'
                    np.savetxt(filename,times, fmt='%.4e')
