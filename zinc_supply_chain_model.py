"""
Zinc Supply Chain Optimization Model – Pyomo

Mathematical model: Multi-period deterministic supply chain with renewable energy

Data source: zinc_case_generated_data_final.xlsx
"""

import pyomo.environ as pyo
import pandas as pd
import sys
import os
import matplotlib
matplotlib.use('Agg')  # non-interactive backend — no display needed
import matplotlib.pyplot as plt


# LOGGING SETUP — tees all print() output to both console and a log file.
# The log file is created in the same directory as this script.
class _Tee:
    """Writes to both a file and the original stdout simultaneously."""
    def __init__(self, filepath):
        self._file = open(filepath, "w", encoding="utf-8", buffering=1)
        self._stdout = sys.stdout
    def write(self, data):
        self._stdout.write(data)
        self._file.write(data)
    def flush(self):
        self._stdout.flush()
        self._file.flush()
    def close(self):
        self._file.close()

_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zinc_model_output.log")
sys.stdout = _Tee(_log_path)
print(f"Logging all output to: {_log_path}")


# DATA LOADING HELPERS


EXCEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zinc_data.xlsx")

def load_excel():
    """Load all sheets into a dict of DataFrames."""
    xl = pd.ExcelFile(EXCEL_PATH)
    sheets = {}
    for name in xl.sheet_names:
        sheets[name] = xl.parse(name)
    return sheets


# MODEL CREATION


def build_model():
    data = load_excel()
    model = pyo.ConcreteModel(name="ZincSupplyChain")

    # 1. SETS
    
    # I  – Mines / concentrate producers  (12 mines)
    I_list = [f"i{n}" for n in range(1, 13)]
    model.I = pyo.Set(initialize=I_list, doc="Mines")

    # J  – Potential warehouse locations  (15 warehouses)
    J_list = [f"w{n}" for n in range(1, 16)]
    model.J = pyo.Set(initialize=J_list, doc="Warehouses")

    # K  – Demand centres (smelters / export ports)  (7 centres)
    K_list = [f"k{n}" for n in range(1, 8)]
    model.K = pyo.Set(initialize=K_list, doc="Demand centres")

    # P  – Product grades  (1=low, 2=medium, 3=high)
    model.P = pyo.Set(initialize=[1, 2, 3], doc="Product grades")

    # T  – Time periods  (6 bi-monthly periods)
    model.T = pyo.Set(initialize=list(range(1, 7)), ordered=True, doc="Time periods")

    # L  – Mine capacity levels
    model.L = pyo.Set(initialize=["small", "medium", "large"], doc="Mine capacity levels")

    # M  – Transport modes
    model.M = pyo.Set(initialize=["truck", "rail"], doc="Transport modes")

    # R  – Renewable energy types
    model.R = pyo.Set(initialize=["wind", "solar"], doc="Renewable energy types")

    # O  – Renewable capacity levels (kW)
    model.O = pyo.Set(initialize=[200, 400, 800], doc="Renewable capacity levels (kW)")

    # G  – Pollutant types
    model.G = pyo.Set(initialize=["CO2", "NOx", "VOC"], doc="Pollutants")

    # C  – Social indicator types
    model.C = pyo.Set(
        initialize=["turnover", "job_creation", "social_acceptance"],
        doc="Social indicators",
    )

    # M(j) – Available transport modes per warehouse
    #         derived from rail_access column in HoldingCost sheet
    hc_df = data["HoldingCost"].set_index("j")
    M_j = {}
    for j in J_list:
        if j in hc_df.index and hc_df.loc[j, "rail_access"] == 1:
            M_j[j] = ["truck", "rail"]
        else:
            M_j[j] = ["truck"]

    model.M_j = pyo.Set(
        model.J,
        initialize=M_j,
        doc="Available transport modes for warehouse j",
    )

    # B  – Candidate recovery facility locations (3 facilities)
    rc_cand_df = data["RecoveryCandidates"].set_index("b")
    B_list = list(data["RecoveryCandidates"]["b"].astype(str))
    model.B = pyo.Set(initialize=B_list, doc="Recovery facility candidates")

    # K_smelter – Smelter demand centres (subset of K, produce jarosite)
    sf_df = data["SmelterFlag"].set_index("k")
    K_smelter_list = [k for k in K_list if int(sf_df.loc[k, "is_smelter"]) == 1]
    model.K_smelter = pyo.Set(within=model.K, initialize=K_smelter_list,
        doc="Smelter demand centres (k1-k5)")

    
    # 2. PARAMETERS
    

    # --- Mine & Production ---------------------------------------------------

    # cp_{i,l}  Production capacity of mine i at level l  (tonnes/period)
    mc_df = data["MineCapacity"].set_index("i")
    cp_data = {}
    for i in I_list:
        for l, col in zip(["small", "medium", "large"], ["cp_small", "cp_medium", "cp_large"]):
            cp_data[(i, l)] = float(mc_df.loc[i, col])

    model.cp = pyo.Param(
        model.I, model.L,
        initialize=cp_data,
        doc="Production capacity of mine i at level l (tonnes/period)",
    )

    # fm_{i,l}  Fixed cost of operating mine i at level l  ($/period)
    # Loaded from MineCapacity sheet, columns fm_small / fm_medium / fm_large.
    fm_data = {}
    for i in I_list:
        for l, col in zip(["small", "medium", "large"], ["fm_small", "fm_medium", "fm_large"]):
            fm_data[(i, l)] = float(mc_df.loc[i, col])

    model.fm = pyo.Param(
        model.I, model.L,
        initialize=fm_data,
        doc="Fixed operating cost of mine i at level l ($/period)",
    )

    # pc_{i,p}  Production cost per tonne of product p at mine i  ($/tonne, excl. energy)
    pcost_df = data["ProductionCost"].set_index("i")
    pc_data = {}
    for i in I_list:
        for p, col in zip([1, 2, 3], ["pc_1", "pc_2", "pc_3"]):
            pc_data[(i, p)] = float(pcost_df.loc[i, col])

    model.pc = pyo.Param(
        model.I, model.P,
        initialize=pc_data,
        doc="Production cost per tonne (excl. energy) at mine i for product p ($/t)",
    )

    # a_p  Energy required to produce one tonne of product p  (kWh/tonne)
    pe_df = data["ProductEnergy"].set_index("p")
    model.a = pyo.Param(
        model.P,
        initialize={int(p): float(pe_df.loc[p, "a"]) for p in pe_df.index},
        doc="Energy intensity of product p (kWh/tonne)",
    )

    # --- Warehouses ----------------------------------------------------------

    # fw_j  Fixed construction/operating cost of warehouse j  ($/period)
    model.fw = pyo.Param(
        model.J,
        initialize={j: float(hc_df.loc[j, "fw"]) for j in J_list},
        doc="Fixed cost of warehouse j ($/period)",
    )

    # ch_j  Storage capacity of warehouse j  (tonnes)
    model.ch = pyo.Param(
        model.J,
        initialize={j: float(hc_df.loc[j, "ch"]) for j in J_list},
        doc="Storage capacity of warehouse j (tonnes)",
    )

    # ca_j  Throughput capacity of warehouse j  (tonnes/period)
    model.ca = pyo.Param(
        model.J,
        initialize={j: float(hc_df.loc[j, "ca"]) for j in J_list},
        doc="Throughput capacity of warehouse j (tonnes/period)",
    )

    # hc_{j,p}  Holding cost per tonne of product p at warehouse j  ($/tonne-period)
    hc_data = {}
    for j in J_list:
        for p, col in zip([1, 2, 3], ["hc_1", "hc_2", "hc_3"]):
            hc_data[(j, p)] = float(hc_df.loc[j, col])

    model.hc = pyo.Param(
        model.J, model.P,
        initialize=hc_data,
        doc="Holding cost of product p at warehouse j ($/tonne-period)",
    )

    # H0_{j,p}  Initial inventory of product p at warehouse j  (tonnes)
    inv_df = data["InitialInventory"]
    # Normalise index keys to lower-case to match J_list
    H0_data = {}
    for _, row in inv_df.iterrows():
        j_key = str(row["j"]).lower()  # 'W1' -> 'w1'
        H0_data[(j_key, int(row["p"]))] = float(row["H0"])

    # Ensure all (j,p) pairs are present (default 0)
    for j in J_list:
        for p in [1, 2, 3]:
            H0_data.setdefault((j, p), 0.0)

    model.H0 = pyo.Param(
        model.J, model.P,
        initialize=H0_data,
        default=0.0,
        doc="Initial inventory of product p at warehouse j (tonnes)",
    )

    # --- Transport distances & costs -----------------------------------------

    # dist_I_J_{i,j}  Road distance mine i → warehouse j  (km)
    dij_df = data["Dist_I_J"].set_index("i")
    dist_IJ_data = {}
    for i in I_list:
        for j in J_list:
            dist_IJ_data[(i, j)] = float(dij_df.loc[i, j])

    model.dist_IJ = pyo.Param(
        model.I, model.J,
        initialize=dist_IJ_data,
        doc="Distance mine i → warehouse j (km)",
    )

    # dist_JK_{j,k,m}  Distance warehouse j → demand centre k by mode m  (km)
    djk_truck = data["Dist_J_K_truck"].set_index("j")
    djk_rail  = data["Dist_J_K_rail"].set_index("j")

    dist_JK_data = {}
    for j in J_list:
        for k in K_list:
            dist_JK_data[(j, k, "truck")] = float(djk_truck.loc[j, k])
            dist_JK_data[(j, k, "rail")]  = float(djk_rail.loc[j, k])

    model.dist_JK = pyo.Param(
        model.J, model.K, model.M,
        initialize=dist_JK_data,
        doc="Distance warehouse j → demand centre k by mode m (km)",
    )

    # ct_mode_m  Transport cost per tonne-km by mode m  ($/tonne-km)
    tp_df = data["TransportParams"].set_index("parameter")
    model.ct_mode = pyo.Param(
        model.M,
        initialize={
            "truck": float(tp_df.loc["ct_mode_truck", "value"]),
            "rail":  float(tp_df.loc["ct_mode_rail",  "value"]),
        },
        doc="Transport cost per tonne-km by mode m ($/tonne-km)",
    )

    # --- Transport capacities ------------------------------------------------

    # cap_mode_{j,m}  Total capacity of mode m at warehouse j  (tonnes/period)
    # Truck aggregate capacity = 1,000,000 everywhere — never binding, not loaded.
    # Only rail values are used in C11; missing truck entries default to 0.
    capmode_df = data["Cap_Mode"].set_index("j")
    cap_mode_data = {}
    for j in J_list:
        cap_mode_data[(j, "rail")] = float(capmode_df.loc[j, "rail"])

    model.cap_mode = pyo.Param(
        model.J, model.M,
        initialize=cap_mode_data,
        default=0.0,
        doc="Total capacity of mode m at warehouse j (tonnes/period)",
    )

    # cap_od_{j,k,m}  OD capacity warehouse j -> demand centre k by mode m  (tonnes/period)
    # Truck OD capacity = 1,000,000 everywhere — never binding, not loaded.
    # Only rail values are loaded; missing truck entries default to 0.
    capod_rail  = data["Cap_OD_rail"].set_index("j")

    cap_od_data = {}
    for j in J_list:
        for k in K_list:
            cap_od_data[(j, k, "rail")] = float(capod_rail.loc[j, k])

    model.cap_od = pyo.Param(
        model.J, model.K, model.M,
        initialize=cap_od_data,
        default=0.0,
        doc="OD capacity from warehouse j to centre k by mode m (tonnes/period)",
    )

    # --- Fixed transport cost ------------------------------------------------

    # ft_{j,m}  Fixed cost of using mode m at warehouse j per period  ($)
    # Truck fixed cost = 0 everywhere — not loaded, defaults to 0.
    # Only rail values contribute to the objective.
    ft_df = data["FixedRailCost"].set_index("j")
    ft_data = {}
    for j in J_list:
        ft_data[(j, "rail")] = float(ft_df.loc[j, "rail"])

    model.ft = pyo.Param(
        model.J, model.M,
        initialize=ft_data,
        default=0.0,
        doc="Fixed cost of using mode m at warehouse j per period ($)",
    )

    # --- Demand & shortage ---------------------------------------------------

    # d_{k,p}^t  Demand of centre k for product p in period t  (tonnes)
    dem_df = data["Demand"]
    dem_data = {}
    for _, row in dem_df.iterrows():
        dem_data[(str(row["k"]), int(row["p"]), int(row["t"]))] = float(row["d"])

    model.d = pyo.Param(
        model.K, model.P, model.T,
        initialize=dem_data,
        doc="Demand of centre k for product p in period t (tonnes)",
    )

    # pc_short  Penalty per tonne of unmet demand  ($/tonne)
    model.pc_short = pyo.Param(
        initialize=float(tp_df.loc["pc_short", "value"]),
        doc="Shortage penalty ($/tonne)",
    )

    # --- Energy & renewables -------------------------------------------------

    # ce_conv  Conventional electricity cost  ($/kWh)
    en_df = data["EnergyCosts"].set_index("parameter")
    model.ce_conv = pyo.Param(
        initialize=float(en_df.loc["ce_conv", "value"]),
        doc="Conventional electricity cost ($/kWh)",
    )

    # ce_ren  Renewable electricity cost  ($/kWh)
    model.ce_ren = pyo.Param(
        initialize=float(en_df.loc["ce_ren", "value"]),
        doc="Renewable electricity cost ($/kWh)",
    )

    # cr_{r,o}  Capacity of renewable system r at level o  (kW)
    rc_df = data["RenewableCapacity"]
    cr_data = {}
    for _, row in rc_df.iterrows():
        cr_data[(str(row["r"]), int(row["o"]))] = float(row["cr"])

    model.cr = pyo.Param(
        model.R, model.O,
        initialize=cr_data,
        doc="Capacity of renewable system r at level o (kW)",
    )

    # inv_{r,o}  Investment cost of renewable system r at level o  ($)
    ri_df = data["RenewableInvestment"]
    inv_data = {}
    for _, row in ri_df.iterrows():
        inv_data[(str(row["r"]), int(row["o"]))] = float(row["inv"])

    model.inv = pyo.Param(
        model.R, model.O,
        initialize=inv_data,
        doc="Investment cost of renewable system r at level o ($)",
    )

    # ea_{i,r}^t  Expected available fraction of renewable r at mine i in period t  [0,1]
    ren_df = data["Renewables"]
    ea_data = {}
    for _, row in ren_df.iterrows():
        ea_data[(str(row["i"]), str(row["r"]), int(row["t"]))] = float(row["ea"])

    model.ea = pyo.Param(
        model.I, model.R, model.T,
        initialize=ea_data,
        doc="Available fraction of renewable r at mine i in period t [0,1]",
    )

    # --- Environment ---------------------------------------------------------

    # emission_{m,g}  Emission factor of pollutant g for mode m  (kg/tonne-km)
    em_df = data["EmissionFactors"].set_index("m")
    emission_data = {}
    for m in ["truck", "rail"]:
        for g in ["CO2", "NOx", "VOC"]:
            emission_data[(m, g)] = float(em_df.loc[m, g])

    model.emission = pyo.Param(
        model.M, model.G,
        initialize=emission_data,
        doc="Emission factor of pollutant g for mode m (kg/tonne-km)",
    )

    # uec_g  Unit environmental cost of pollutant g  ($/kg)
    uec_df = data["UnitEnvCost"].set_index("g")
    model.uec = pyo.Param(
        model.G,
        initialize={g: float(uec_df.loc[g, "uec"]) for g in ["CO2", "NOx", "VOC"]},
        doc="Unit environmental cost of pollutant g ($/kg)",
    )

    # UB^E  Environmental cost upper bound per period  ($)
    model.UB_E = pyo.Param(
        initialize=float(tp_df.loc["UB_E", "value"]),
        mutable=True,   # mutable so sensitivity runs can call set_value()
        doc="Environmental cost upper bound per period ($)",
    )

    # --- Social --------------------------------------------------------------

    # rho_{c,i,l}  Social indicator score c for mine i at capacity level l
    soc_df = data["Social"]
    rho_data = {}
    for _, row in soc_df.iterrows():
        i_key = str(row["i"])
        l_key = str(row["l"])
        for c in ["turnover", "job_creation", "social_acceptance"]:
            rho_data[(c, i_key, l_key)] = float(row[c])

    model.rho = pyo.Param(
        model.C, model.I, model.L,
        initialize=rho_data,
        doc="Social indicator score c for mine i at capacity level l",
    )

    # nu_c  Weight of social indicator c
    sw_df = data["SocialWeights"].set_index("c")
    model.nu = pyo.Param(
        model.C,
        initialize={c: float(sw_df.loc[c, "nu"])
                    for c in ["turnover", "job_creation", "social_acceptance"]},
        doc="Weight of social indicator c",
    )

    # LB^S  Minimum acceptable social score per period
    model.LB_S = pyo.Param(
        initialize=float(tp_df.loc["LB_S", "value"]),
        mutable=True,   # mutable so sensitivity runs can call set_value()
        doc="Minimum acceptable social score per period",
    )

    # --- Policy --------------------------------------------------------------

    # alpha  Minimum renewable energy share  [0,1]
    model.alpha = pyo.Param(
        initialize=float(tp_df.loc["alpha", "value"]),
        mutable=True,   # mutable so sensitivity runs can call set_value()
        doc="Minimum renewable energy share [0,1]",
    )

    
    # 2b. REVERSE LOGISTICS PARAMETERS
    

    rp_df   = data["RecoveryParams"].set_index("parameter")
    rd_df   = data["ReverseDist"].set_index("k")

    waste_rate_val   = float(rp_df.loc["waste_rate",         "value"])
    zinc_content_val = float(rp_df.loc["zinc_content",       "value"])
    rec_eff_val      = float(rp_df.loc["rec_eff",            "value"])
    rev_zn_val       = float(rp_df.loc["rev_zn",             "value"])
    cost_proc_val    = float(rp_df.loc["cost_proc",          "value"])
    cost_disp_val    = float(rp_df.loc["cost_disp",          "value"])
    env_disp_val     = float(rp_df.loc["env_disp",           "value"])
    ct_rev_val       = float(rp_df.loc["ct_rev",             "value"])
    beta_min_val     = float(rp_df.loc["beta_min_recovery",  "value"])

    model.waste_rate   = pyo.Param(initialize=waste_rate_val,
        doc="Jarosite generation rate (t waste / t concentrate)")
    model.zinc_content = pyo.Param(initialize=zinc_content_val,
        doc="Zinc mass fraction in jarosite")
    model.rec_eff      = pyo.Param(initialize=rec_eff_val,
        doc="Zinc recovery efficiency")
    model.rev_zn       = pyo.Param(initialize=rev_zn_val, mutable=True,
        doc="Revenue per tonne recovered zinc ($/t)")
    model.cost_proc    = pyo.Param(initialize=cost_proc_val,
        doc="Processing cost per tonne jarosite ($/t)")
    model.cost_disp    = pyo.Param(initialize=cost_disp_val,
        doc="Direct disposal cost ($/t)")
    model.env_disp     = pyo.Param(initialize=env_disp_val, mutable=True,
        doc="Environmental penalty for disposal ($/t)")
    model.ct_rev       = pyo.Param(initialize=ct_rev_val,
        doc="Reverse truck transport cost ($/t-km)")
    model.beta_min     = pyo.Param(initialize=beta_min_val, mutable=True,
        doc="Minimum jarosite recovery share")

    fw_rec_data  = {b: float(rc_cand_df.loc[b, "fw_rec"])  for b in B_list}
    cap_rec_data = {b: float(rc_cand_df.loc[b, "cap_rec"]) for b in B_list}

    model.fw_rec  = pyo.Param(model.B, initialize=fw_rec_data,
        doc="Fixed cost of recovery facility b ($/period)")
    model.cap_rec = pyo.Param(model.B, initialize=cap_rec_data,
        doc="Processing capacity of recovery facility b (t/period)")

    b_col_map = {"b1": "b1_Bandar_Abbas", "b2": "b2_Zanjan", "b3": "b3_Bafgh"}
    dist_rev_data = {
        (k, b): float(rd_df.loc[k, b_col_map[b]])
        for k in K_smelter_list for b in B_list
    }
    model.dist_rev = pyo.Param(model.K_smelter, model.B,
        initialize=dist_rev_data,
        doc="Distance from smelter k to recovery facility b (km)")

    
    # 3. PRECOMPUTED BOUNDS  
    
    # Compute tight upper bounds for continuous variables from parameter data. (for a faster computing speed)
    # Tighter bounds shrink the LP relaxation and help the solver significantly.

    # Max production capacity per mine (largest level)
    max_cp = {
        i: max(cp_data[(i, l)] for l in ["small", "medium", "large"])
        for i in I_list
    }

    # Max renewable generation per (mine, source, period)
    # = availability fraction * largest installed capacity * hours per period
    HOURS_PER_PERIOD = 1460
    max_cr = {r: max(cr_data[(r, o)] for o in [200, 400, 800]) for r in ["wind", "solar"]}
    ea_data_local = {}
    ren_df2 = data["Renewables"]
    for _, row in ren_df2.iterrows():
        ea_data_local[(str(row["i"]), str(row["r"]), int(row["t"]))] = float(row["ea"])

    max_Q = {
        (i, r, t): ea_data_local[(i, r, t)] * max_cr[r] * HOURS_PER_PERIOD
        for i in I_list for r in ["wind", "solar"] for t in range(1, 7)
    }

    # Max conventional energy per mine per period = total energy at max capacity
    # a_p values: p=1->200, p=2->300, p=3->350
    a_vals = {1: 200.0, 2: 300.0, 3: 350.0}
    max_S = {i: sum(a_vals[p] for p in [1, 2, 3]) * max_cp[i] for i in I_list}

    # Max FD flow per (j,k,m):
    # Truck: cap_od_truck = 1,000,000 everywhere — always dominated by ca[j]=6,000.
    #        Bound is simply ca[j]; Cap_OD_truck sheet not loaded.
    # Rail:  cap_od_rail varies; bound is min(ca[j], cap_od_rail[j,k]).
    capod_rail2 = data["Cap_OD_rail"].set_index("j")
    ca_vals     = {j: float(hc_df.loc[j, "ca"]) for j in J_list}
    max_FD = {}
    for j in J_list:
        for k in K_list:
            max_FD[(j, k, "truck")] = ca_vals[j]
            max_FD[(j, k, "rail")]  = min(ca_vals[j], float(capod_rail2.loc[j, k]))

    # ct_IJ[i,j]   = ct_mode_truck * dist_IJ[i,j]
    # ct_JK[j,k,m] = ct_mode_m * dist_JK[j,k,m]
    # Defined here (section 3) so the objective function in section 5 can use them.
    ct_truck_val = float(tp_df.loc["ct_mode_truck", "value"])
    ct_rail_val  = float(tp_df.loc["ct_mode_rail",  "value"])
    ct_IJ = {
        (i, j): ct_truck_val * dist_IJ_data[(i, j)]
        for i in I_list for j in J_list
    }
    ct_JK = {
        (j, k, m): (ct_truck_val if m == "truck" else ct_rail_val) * dist_JK_data[(j, k, m)]
        for j in J_list for k in K_list for m in ["truck", "rail"]
    }

    # ct_rev_dist[k,b] = ct_rev * dist_rev[k,b]  (precomputed for objective)
    ct_rev_dist = {
        (k, b): ct_rev_val * dist_rev_data[(k, b)]
        for k in K_smelter_list for b in B_list
    }

    # max_jarosite_k[k] = worst-case jarosite from smelter k in one period
    max_jarosite_k = {
        k: waste_rate_val * max(
            sum(dem_data.get((k, p, t), 0.0) for p in [1, 2, 3])
            for t in range(1, 7)
        )
        for k in K_smelter_list
    }

    
    # 4. DECISION VARIABLES
    

    # X_j  Binary: 1 if warehouse j is opened
    model.X = pyo.Var(
        model.J,
        within=pyo.Binary,
        doc="1 if warehouse j is opened",
    )

    # Y_{i,r,o}  Binary: 1 if renewable system r at level o is installed at mine i
    model.Y = pyo.Var(
        model.I, model.R, model.O,
        within=pyo.Binary,
        doc="1 if renewable r at level o is installed at mine i",
    )

    # Z_{i,l}^t  Binary: 1 if mine i operates at capacity level l in period t
    model.Z = pyo.Var(
        model.I, model.L, model.T,
        within=pyo.Binary,
        doc="1 if mine i operates at capacity level l in period t",
    )

    # V_{i,p}^t  Production of product p at mine i in period t  (tonnes)
    # Upper bound: max capacity level of that mine  
    model.V = pyo.Var(
        model.I, model.P, model.T,
        bounds=lambda model, i, p, t: (0, max_cp[i]),
        doc="Production of product p at mine i in period t (tonnes)",
    )

    # U_{k,p}^t  Unmet demand of product p at centre k in period t  (tonnes)
    model.U = pyo.Var(
        model.K, model.P, model.T,
        within=pyo.NonNegativeReals,
        doc="Unmet demand of product p at centre k in period t (tonnes)",
    )

    # FW_{i,j,p}^t  Flow from mine i to warehouse j for product p in period t  (tonnes)
    # Upper bound: max capacity of the source mine 
    model.FW = pyo.Var(
        model.I, model.J, model.P, model.T,
        bounds=lambda model, i, j, p, t: (0, max_cp[i]),
        doc="Flow mine i → warehouse j for product p in period t (tonnes)",
    )

    # FD_{j,k,m,p}^t  Flow from warehouse j to centre k by mode m for product p in period t  (tonnes)
    # Upper bound: min(warehouse throughput, OD capacity) 
    model.FD = pyo.Var(
        model.J, model.K, model.M, model.P, model.T,
        bounds=lambda model, j, k, m, p, t: (0, max_FD[(j, k, m)]),
        doc="Flow warehouse j → centre k by mode m for product p in period t (tonnes)",
    )

    # H_{j,p}^t  Inventory of product p at warehouse j at end of period t  (tonnes)
    model.H = pyo.Var(
        model.J, model.P, model.T,
        within=pyo.NonNegativeReals,
        doc="Inventory of product p at warehouse j at end of period t (tonnes)",
    )

    # W_{j,m}^t  Binary: 1 if mode m is used at warehouse j in period t
    # Declared over all (J, M, T) then fixed to 0 for unavailable mode/warehouse
    # combinations. This keeps the rest of the code simple while removing those
    # variables from the branch-and-bound tree (improvement 2).
    model.W = pyo.Var(
        model.J, model.M, model.T,
        within=pyo.Binary,
        doc="1 if transport mode m is used at warehouse j in period t",
    )
    # Fix W to 0 for unavailable modes AND for truck at all warehouses.
    # Truck W variables are disconnected from the model: there are no truck
    # capacity constraints that use W_truck (unlike rail which has C11/C12),
    # and ft[j,"truck"]=0 so they add no cost either. Fixing all 90 truck W
    # variables removes them from the B&B tree entirely.
    for _j in J_list:
        for _t in range(1, 7):
            model.W[_j, "truck", _t].fix(0)          # truck: always disconnected
            if "rail" not in M_j[_j]:
                model.W[_j, "rail", _t].fix(0)        # rail: not available here

    # Q_{i,r}^t  Renewable energy received from source r at mine i in period t  (kWh)
    # Upper bound: availability * max installed capacity * hours per period 
    model.Q = pyo.Var(
        model.I, model.R, model.T,
        bounds=lambda model, i, r, t: (0, max_Q[(i, r, t)]),
        doc="Renewable energy from source r at mine i in period t (kWh)",
    )

    # S_{i}^t  Conventional energy consumed at mine i in period t  (kWh)
    # Upper bound: total energy needed if mine runs at max capacity  
    model.S = pyo.Var(
        model.I, model.T,
        bounds=lambda model, i, t: (0, max_S[i]),
        doc="Conventional energy consumed at mine i in period t (kWh)",
    )

    
    # 4b. REVERSE LOGISTICS VARIABLES
    

    # XR_b  Binary: 1 if recovery facility b is built
    model.XR = pyo.Var(model.B, within=pyo.Binary,
        doc="1 if recovery facility b is built")

    # FW_rev[k,b,t]  Jarosite flow from smelter k to facility b in period t (t)
    model.FW_rev = pyo.Var(model.K_smelter, model.B, model.T,
        bounds=lambda model, k, b, t: (0, max_jarosite_k[k]),
        doc="Jarosite flow smelter k -> facility b in period t (t)")

    # W_proc[b,t]  Jarosite processed at facility b in period t (t)
    model.W_proc = pyo.Var(model.B, model.T,
        bounds=lambda model, b, t: (0, cap_rec_data[b]),
        doc="Jarosite processed at recovery facility b in period t (t)")

    # W_disp[k,t]  Jarosite disposed directly from smelter k in period t (t)
    model.W_disp = pyo.Var(model.K_smelter, model.T,
        bounds=lambda model, k, t: (0, max_jarosite_k[k]),
        doc="Jarosite disposed directly from smelter k in period t (t)")

    # R_zn[b,t]  Zinc recovered at facility b in period t (t)
    model.R_zn = pyo.Var(model.B, model.T,
        bounds=lambda model, b, t: (0, zinc_content_val * rec_eff_val * cap_rec_data[b]),
        doc="Zinc recovered at facility b in period t (t)")

    
    # 5. OBJECTIVE FUNCTION – Minimise total cost
    
    #
    # Total cost = warehouse fixed costs
    #            + renewable investment costs
    #            + mine fixed operating costs
    #            + transport mode fixed costs
    #            + inventory holding costs
    #            + mine-to-warehouse transport costs  (truck only)
    #            + warehouse-to-demand transport costs (all modes)
    #            + production costs (excl. energy)
    #            + conventional energy costs
    #            + renewable energy costs
    #            + shortage penalty costs

    def total_cost(model):

        # 1) Warehouse fixed costs: sum_j fw_j * X_j
        warehouse_fixed = sum(
            model.fw[j] * model.X[j]
            for j in model.J
        )

        # 2) Renewable investment costs: sum_{i,r,o} inv_{r,o} * Y_{i,r,o}
        renewable_invest = sum(
            model.inv[r, o] * model.Y[i, r, o]
            for i in model.I
            for r in model.R
            for o in model.O
        )

        # 3) Mine fixed operating costs: sum_{t,i,l} fm_{i,l} * Z_{i,l}^t
        mine_fixed = sum(
            model.fm[i, l] * model.Z[i, l, t]
            for t in model.T
            for i in model.I
            for l in model.L
        )

        # 4) Transport mode fixed costs (rail only — ft[j,truck]=0 and W_truck fixed to 0)
        transport_fixed = sum(
            model.ft[j, "rail"] * model.W[j, "rail", t]
            for t in model.T
            for j in model.J
            if "rail" in M_j[j]
        )

        # 5) Inventory holding costs: sum_{t,j,p} hc_{j,p} * H_{j,p}^t
        holding = sum(
            model.hc[j, p] * model.H[j, p, t]
            for t in model.T
            for j in model.J
            for p in model.P
        )

        # 6) Mine → warehouse transport costs (truck only):
        #    ct_IJ precomputed in section 6 below
        transport_IJ = sum(
            ct_IJ[i, j] * model.FW[i, j, p, t]
            for t in model.T
            for i in model.I
            for j in model.J
            for p in model.P
        )

        # 7) Warehouse → demand centre transport costs:
        #    ct_JK precomputed in section 6 below
        transport_JK = sum(
            ct_JK[j, k, m] * model.FD[j, k, m, p, t]
            for t in model.T
            for j in model.J
            for k in model.K
            for m in model.M
            for p in model.P
        )

        # 8) Production costs (excl. energy):
        #    sum_{t,i,p} pc_{i,p} * V_{i,p}^t
        production = sum(
            model.pc[i, p] * model.V[i, p, t]
            for t in model.T
            for i in model.I
            for p in model.P
        )

        # 9) Conventional energy costs:
        #    sum_{t,i} ce_conv * S_{i}^t
        conv_energy = sum(
            model.ce_conv * model.S[i, t]
            for t in model.T
            for i in model.I
        )

        # 10) Renewable energy costs:
        #     sum_{t,i,r} ce_ren * Q_{i,r}^t
        ren_energy = sum(
            model.ce_ren * model.Q[i, r, t]
            for t in model.T
            for i in model.I
            for r in model.R
        )

        # 11) Shortage penalty:
        #     sum_{t,k,p} pc_short * U_{k,p}^t
        shortage = sum(
            model.pc_short * model.U[k, p, t]
            for t in model.T
            for k in model.K
            for p in model.P
        )


        # 12) Recovery facility fixed costs
        recovery_fixed = sum(model.fw_rec[b] * model.XR[b] for b in model.B)

        # 13) Reverse transport costs
        rev_transport = sum(
            ct_rev_dist[k, b] * model.FW_rev[k, b, t]
            for t in model.T for k in model.K_smelter for b in model.B
        )

        # 14) Jarosite processing costs
        processing = sum(
            model.cost_proc * model.W_proc[b, t]
            for t in model.T for b in model.B
        )

        # 15) Disposal costs (direct + environmental penalty)
        disposal = sum(
            (model.cost_disp + model.env_disp) * model.W_disp[k, t]
            for t in model.T for k in model.K_smelter
        )

        # 16) Recovered zinc revenue (subtracted)
        zn_revenue = sum(
            model.rev_zn * model.R_zn[b, t]
            for t in model.T for b in model.B
        )

        return (
            warehouse_fixed + renewable_invest + mine_fixed + transport_fixed
            + holding + transport_IJ + transport_JK + production
            + conv_energy + ren_energy + shortage
            + recovery_fixed + rev_transport + processing + disposal
            - zn_revenue
        )

    model.OBJ = pyo.Objective(rule=total_cost, sense=pyo.minimize)

    
    # 6. PRECOMPUTED CONSTRAINT COEFFICIENTS
    

    # nu_rho_sum[i,l] = sum_c nu_c * rho[c,i,l]  (weighted, used in C15)
    # Precomputing this collapses the C loop in C15 into a single coefficient.
    sw_df2 = data["SocialWeights"].set_index("c")
    nu_vals = {c: float(sw_df2.loc[c, "nu"])
               for c in ["turnover", "job_creation", "social_acceptance"]}
    nu_rho_sum = {
        (i, l): sum(nu_vals[c] * rho_data[(c, i, l)]
                    for c in ["turnover", "job_creation", "social_acceptance"])
        for i in I_list for l in ["small", "medium", "large"]
    }
    model._nu_rho_sum = nu_rho_sum   # stored for use in results printout

    # coeff_IJ[i,j] = sum_g uec[g] * emission[truck,g] * dist_IJ[i,j]  
    # Collapses the 3-pollutant loop in C14 and objective into a single scalar.
    uec_vals      = {g: float(uec_df.loc[g, "uec"])    for g in ["CO2", "NOx", "VOC"]}
    emission_vals = {(m, g): float(em_df.loc[m, g])
                     for m in ["truck", "rail"] for g in ["CO2", "NOx", "VOC"]}

    coeff_IJ = {
        (i, j): sum(
            uec_vals[g] * emission_vals[("truck", g)] * dist_IJ_data[(i, j)]
            for g in ["CO2", "NOx", "VOC"]
        )
        for i in I_list for j in J_list
    }

    coeff_JK = {
        (j, k, m): sum(
            uec_vals[g] * emission_vals[(m, g)] * dist_JK_data[(j, k, m)]
            for g in ["CO2", "NOx", "VOC"]
        )
        for j in J_list for k in K_list for m in ["truck", "rail"]
    }

    # Store on model so env_cost_period() can access them after build
    model._coeff_IJ = coeff_IJ
    model._coeff_JK = coeff_JK

    
    # 7. CONSTRAINTS
    

    
    # C1 – Mine capacity level selection
    # Exactly one capacity level must be chosen for each mine in each period.
    # sum_{l in L} Z_{i,l}^t = 1    for all i in I, t in T0
    
    def C1_mine_level_selection(model, i, t):
        return sum(model.Z[i, l, t] for l in model.L) == 1

    model.C1 = pyo.Constraint(model.I, model.T, rule=C1_mine_level_selection)

    
    # C2 – Production capacity
    # Total production across products cannot exceed capacity of chosen level.
    # sum_{p in P} V_{i,p}^t <= sum_{l in L} cp_{i,l} * Z_{i,l}^t
    #                                            for all i in I, t in T
    
    def C2_production_capacity(model, i, t):
        return (
            sum(model.V[i, p, t] for p in model.P)
            <=
            sum(model.cp[i, l] * model.Z[i, l, t] for l in model.L)
        )

    model.C2 = pyo.Constraint(model.I, model.T, rule=C2_production_capacity)

    
    # C3 – Mine flow conservation
    # All production at a mine must be shipped out to warehouses.
    # sum_{j in J} FW_{i,j,p}^t = V_{i,p}^t
    #                                 for all i in I, p in P, t in T
    
    def C3_mine_flow_conservation(model, i, p, t):
        return (
            sum(model.FW[i, j, p, t] for j in model.J)
            ==
            model.V[i, p, t]
        )

    model.C3 = pyo.Constraint(model.I, model.P, model.T, rule=C3_mine_flow_conservation)

    
    # C4 – Energy balance at mine
    # Total energy demand = renewable energy + conventional energy.
    # sum_{p in P} a_p * V_{i,p}^t <= sum_{r in R} Q_{i,r}^t + S_{i}^t
    #                                            for all i in I, t in T
    
    def C4_energy_balance(model, i, t):
        return (
            sum(model.a[p] * model.V[i, p, t] for p in model.P)
            <=
            sum(model.Q[i, r, t] for r in model.R) + model.S[i, t]
        )

    model.C4 = pyo.Constraint(model.I, model.T, rule=C4_energy_balance)

    
    # C5 – Renewable energy generation limit
    # Renewable energy from source r at mine i in period t is bounded by
    # the installed capacity times the availability fraction.
    # Q_{i,r}^t <= ea_{i,r}^t * sum_{o in O} cr_{r,o} * Y_{i,r,o}
    #                                 for all i in I, r in R, t in T
    

    # 2 months ≈ 1460 hours (can be adjusted if needed)
    HOURS_PER_PERIOD = 1460
    model.hours_per_period = pyo.Param(initialize=HOURS_PER_PERIOD, mutable=False,  doc="Hours in one bi‑monthly period")
    def C5_renewable_generation(model, i, r, t):
        return (model.Q[i, r, t] <= model.ea[i, r, t] * sum(model.cr[r, o] * model.hours_per_period * model.Y[i, r, o] for o in model.O))

    model.C5 = pyo.Constraint(model.I, model.R, model.T, rule=C5_renewable_generation)

    
    # C6 – At most one capacity level per renewable type per mine
    # sum_{o in O} Y_{i,r,o} <= 1    for all i in I, r in R
    
    def C6_renewable_level(model, i, r):
        return sum(model.Y[i, r, o] for o in model.O) <= 1

    model.C6 = pyo.Constraint(model.I, model.R, rule=C6_renewable_level)

    
    # C7 – Inventory balance (all periods unified)  
    # For t=1: H_{j,p,1} = H0_{j,p} + inflow - outflow
    # For t>1: H_{j,p,t} = H_{j,p,t-1} + inflow - outflow
    # Both cases handled by using H0 as the "period 0" inventory.
    
    def C7_inventory_balance(model, j, p, t):
        # H0[j,p] = 0 for all j,p in this dataset — hardcoded to avoid
        # parameter lookups. If H0 changes in future, we can restore model.H0[j,p].
        prev_inv = 0 if t == 1 else model.H[j, p, t - 1]
        return (
            model.H[j, p, t]
            ==
            prev_inv
            + sum(model.FW[i, j, p, t] for i in model.I)
            - sum(model.FD[j, k, m, p, t] for k in model.K for m in model.M_j[j])
        )

    model.C7 = pyo.Constraint(model.J, model.P, model.T, rule=C7_inventory_balance)

    
    # C8 – Warehouse storage capacity
    # Total inventory across products cannot exceed warehouse capacity.
    # sum_{p in P} H_{j,p}^t <= ch_j * X_j    for all j in J, t in T
    
    def C8_storage_capacity(model, j, t):
        return (
            sum(model.H[j, p, t] for p in model.P)
            <=
            model.ch[j] * model.X[j]
        )

    model.C8 = pyo.Constraint(model.J, model.T, rule=C8_storage_capacity)

    
    # C9 – Warehouse throughput capacity
    # Total outflow from warehouse cannot exceed its throughput capacity.
    # sum_{p,k,m in M(j)} FD_{j,k,m,p}^t <= ca_j * X_j
    #                                            for all j in J, t in T
    
    def C9_throughput_capacity(model, j, t):
        return (
            sum(
                model.FD[j, k, m, p, t]
                for p in model.P
                for k in model.K
                for m in model.M_j[j]
            )
            <=
            model.ca[j] * model.X[j]
        )

    model.C9 = pyo.Constraint(model.J, model.T, rule=C9_throughput_capacity)

    
    # C10 – Transport mode only used at open warehouses
    # W_{j,rail,t} <= X_j    for all j with rail access, t
    # Truck and non-rail-warehouse rail W variables are already fixed to 0.
    
    def C10_mode_requires_open_warehouse(model, j, m, t):
        if m == "truck":
            return pyo.Constraint.Skip   # W[j,truck,t] fixed to 0 in var section
        if m not in model.M_j[j]:
            return pyo.Constraint.Skip   # W[j,rail,t] fixed to 0 in var section
        return model.W[j, m, t] <= model.X[j]

    model.C10 = pyo.Constraint(model.J, model.M, model.T, rule=C10_mode_requires_open_warehouse)

    
    # C11 – Total rail mode capacity at warehouse
    # sum_{k,p} FD_{j,k,rail,p}^t <= cap_mode_{j,rail} * W_{j,rail}^t
    #                                            for all j in J, t in T
    # (Truck flows are limited by OD capacity in C12; rail has an additional
    #  aggregate cap at the warehouse level)
    
    def C11_rail_mode_capacity(model, j, t):
        # Skip warehouses without rail access 
        if "rail" not in model.M_j[j]:
            return pyo.Constraint.Skip
        return (
            sum(
                model.FD[j, k, "rail", p, t]
                for k in model.K
                for p in model.P
            )
            <=
            model.cap_mode[j, "rail"] * model.W[j, "rail", t]
        )

    model.C11 = pyo.Constraint(model.J, model.T, rule=C11_rail_mode_capacity)

    
    # C12 – OD (origin-destination) rail capacity per warehouse-demand pair
    # sum_{p} FD_{j,k,rail,p}^t <= cap_od_{j,k,rail} * W_{j,rail}^t
    #                                 for all j in J, k in K, t in T
    
    def C12_od_rail_capacity(model, j, k, t):
        # Skip warehouses without rail access 
        if "rail" not in model.M_j[j]:
            return pyo.Constraint.Skip
        return (
            sum(model.FD[j, k, "rail", p, t] for p in model.P)
            <=
            model.cap_od[j, k, "rail"] * model.W[j, "rail", t]
        )

    model.C12 = pyo.Constraint(model.J, model.K, model.T, rule=C12_od_rail_capacity)

    
    # C13 – Demand satisfaction
    # Total flow received at demand centre k + unmet demand = total demand.
    # sum_{j, m in M(j)} FD_{j,k,m,p}^t + U_{k,p}^t = d_{k,p}^t
    #                                 for all k in K, p in P, t in T
    
    def C13_demand_satisfaction(model, k, p, t):
        return (
            sum(
                model.FD[j, k, m, p, t]
                for j in model.J
                for m in model.M_j[j]
            )
            + model.U[k, p, t]
            ==
            model.d[k, p, t]
        )

    model.C13 = pyo.Constraint(model.K, model.P, model.T, rule=C13_demand_satisfaction)

    
    # C14 – Environmental cost upper bound per period
    # sum_{g} uec_g * [
    #     sum_{i,j,p} emission_{truck,g} * dist_IJ_{i,j} * FW_{i,j,p}^t
    #   + sum_{j,k,m in M(j),p} emission_{m,g} * dist_JK_{j,k,m} * FD_{j,k,m,p}^t
    # ] <= UB^E     for all t in T
    
    def C14_environmental_cap(model, t):
        # Uses precomputed coeff_IJ and coeff_JK 
        # the pollutant loop is gone; each term is now one multiplication.
        mine_to_wh = sum(
            coeff_IJ[i, j] * model.FW[i, j, p, t]
            for i in model.I
            for j in model.J
            for p in model.P
        )
        wh_to_dem = sum(
            coeff_JK[j, k, m] * model.FD[j, k, m, p, t]
            for j in model.J
            for k in model.K
            for m in model.M_j[j]
            for p in model.P
        )
        return mine_to_wh + wh_to_dem <= model.UB_E

    model.C14 = pyo.Constraint(model.T, rule=C14_environmental_cap)

    
    # C15 - Minimum weighted social score per period
    # sum_{c,i,l} nu_c * rho_{c,i,l} * Z_{i,l,t} >= LB_S    for all t in T
    # Uses precomputed nu_rho_sum[i,l] = sum_c nu_c * rho[c,i,l] for efficiency.
    
    def C15_social_score(model, t):
        return (
            sum(
                nu_rho_sum[i, l] * model.Z[i, l, t]
                for i in model.I
                for l in model.L
            )
            >=
            model.LB_S
        )

    model.C15 = pyo.Constraint(model.T, rule=C15_social_score)

    
    # C16 – Minimum renewable energy share per period
    # (1 - alpha) * sum_{i,r} Q_{i,r,t} >= alpha * sum_i S_{i,t}    for all t in T
    # (equivalent to Q >= alpha*(Q+S), rewritten to avoid expanding the RHS)
    
    def C16_renewable_share(model, t):
        # Rewritten as (1-alpha)*Q >= alpha*S to avoid expanding alpha*(Q+S)  
        total_renewable = sum(model.Q[i, r, t] for i in model.I for r in model.R)
        total_conv      = sum(model.S[i, t]    for i in model.I)
        return (1 - model.alpha) * total_renewable >= model.alpha * total_conv

    model.C16 = pyo.Constraint(model.T, rule=C16_renewable_share)


    
    # 7b. REVERSE LOGISTICS CONSTRAINTS
    

    
    # C17 – Jarosite generation balance at smelters
    # sum_b FW_rev[k,b,t] + W_disp[k,t] = waste_rate * sum_{j,m,p} FD[j,k,m,p,t]
    
    def C17_jarosite_balance(model, k, t):
        return (
            sum(model.FW_rev[k, b, t] for b in model.B) + model.W_disp[k, t]
            ==
            model.waste_rate * sum(
                model.FD[j, k, m, p, t]
                for j in model.J for m in model.M_j[j] for p in model.P
            )
        )
    model.C17 = pyo.Constraint(model.K_smelter, model.T, rule=C17_jarosite_balance)

    
    # C18 – Flow balance at recovery facility (no in-facility storage)
    # sum_k FW_rev[k,b,t] = W_proc[b,t]    for all b, t
    
    def C18_recovery_flow_balance(model, b, t):
        return (
            sum(model.FW_rev[k, b, t] for k in model.K_smelter)
            == model.W_proc[b, t]
        )
    model.C18 = pyo.Constraint(model.B, model.T, rule=C18_recovery_flow_balance)

    
    # C19 – Recovery facility capacity
    # W_proc[b,t] <= cap_rec_b * XR_b    for all b, t
    
    def C19_recovery_capacity(model, b, t):
        return model.W_proc[b, t] <= model.cap_rec[b] * model.XR[b]
    model.C19 = pyo.Constraint(model.B, model.T, rule=C19_recovery_capacity)

    
    # C20 – Recovered zinc calculation
    # R_zn[b,t] = zinc_content * rec_eff * W_proc[b,t]    for all b, t
    
    def C20_zinc_recovery(model, b, t):
        return model.R_zn[b, t] == model.zinc_content * model.rec_eff * model.W_proc[b, t]
    model.C20 = pyo.Constraint(model.B, model.T, rule=C20_zinc_recovery)

    
    # C21 – Minimum jarosite recovery share per period
    # sum_b W_proc[b,t] >= beta_min * waste_rate * sum_{k,j,m,p} FD[j,k,m,p,t]
    
    def C21_min_recovery_share(model, t):
        return (
            sum(model.W_proc[b, t] for b in model.B)
            >= model.beta_min * model.waste_rate * sum(
                model.FD[j, k, m, p, t]
                for k in model.K_smelter
                for j in model.J for m in model.M_j[j] for p in model.P
            )
        )
    model.C21 = pyo.Constraint(model.T, rule=C21_min_recovery_share)

    return model



# HELPER – compute environmental cost for one period
# (used both in C14 and in the results printout)


def env_cost_period(model, t):
    """Return the total environmental cost for period t (post-solve numeric value).
    Uses precomputed coeff_IJ and coeff_JK stored on the model.
    """
    mine_to_wh = sum(
        model._coeff_IJ[i, j] * pyo.value(model.FW[i, j, p, t])
        for i in model.I
        for j in model.J
        for p in model.P
    )
    wh_to_dem = sum(
        model._coeff_JK[j, k, mode] * pyo.value(model.FD[j, k, mode, p, t])
        for j in model.J
        for k in model.K
        for mode in model.M_j[j]
        for p in model.P
    )
    return mine_to_wh + wh_to_dem



# SOLVE & PRINT  (improvement 4 – wrapped in a function for sensitivity runs)


def solve_and_report(model, label="BASE", verbose=True):
    """
    Solve model, print a full results summary, and return a dict with the
    key KPIs so we can collect them across sensitivity runs.

    Usage for a single run:
        m = build_model()
        kpis = solve_and_report(m)

    Usage for sensitivity (e.g. varying alpha):
        for alpha_val in [0.1, 0.2, 0.3, 0.4, 0.5]:
            m = build_model()
            m.alpha.set_value(alpha_val)
            kpis = solve_and_report(m, label=f"alpha={alpha_val}")
    """
    solver = pyo.SolverFactory("appsi_highs")
    solver.options["mip_rel_gap"] = 0.005  # stop at 0.5% optimality gap
    solver.options["time_limit"] = 600     # hard stop at 600 seconds (because sensitivity analysis can take very long)
    print("\nSolving [" + label + "] ...")
    results = solver.solve(model, tee=True, load_solutions=False)

    
    # Robust status check — works whether appsi returns an enum or a string.
    # We only refuse to report results if the model is truly infeasible/unbounded.
    # maxTimeLimit with a feasible point is perfectly usable.
    
    status_str = str(results.solver.termination_condition).lower()
    print("  Termination condition:", status_str)

    INFEASIBLE_STATUSES = {"infeasible", "infeasibleorunbounded", "unbounded"}
    truly_infeasible = status_str in INFEASIBLE_STATUSES

    kpis = {
        "label": label,
        "status": status_str,
        "obj": None,
        "shortage": None,
        "env_costs": {},
        "social_scores": {},
        "gap": None,
        "zn_recovered": {},
        "total_zn_revenue": 0.0,
        "total_disposal": 0.0,
        "recovery_plants_opened": 0,
    }

    sep = "=" * 60
    if truly_infeasible:
        print("  >> Problem is infeasible or unbounded — no solution to report.")
        return kpis

    # Load the best solution found (works for optimal AND time-limit runs).
    model.solutions.load_from(results)

    # Compute MIP gap from objective value and dual bound stored in results.
    obj_val = pyo.value(model.OBJ)
    try:
        dual_bound = float(results.problem.lower_bound)
        if obj_val is not None and abs(obj_val) > 1e-10:
            gap = abs(obj_val - dual_bound) / abs(obj_val)
        else:
            gap = None
    except Exception:
        gap = None
    kpis["gap"] = gap
    kpis["obj"] = obj_val

    
    # Collect KPIs that are needed regardless of verbose setting
    

    # Unmet demand
    total_shortage = 0.0
    for t in model.T:
        for k in model.K:
            for p in model.P:
                val = pyo.value(model.U[k, p, t])
                if val is not None and val > 0.01:
                    total_shortage += val
    kpis["shortage"] = total_shortage

    # Environmental costs per period
    ub_e = pyo.value(model.UB_E)
    for t in model.T:
        kpis["env_costs"][t] = env_cost_period(model, t)

    # Social scores per period
    lb_s = pyo.value(model.LB_S)
    for t in model.T:
        kpis["social_scores"][t] = sum(
            pyo.value(model.nu[c]) * pyo.value(model.rho[c, i, l]) * pyo.value(model.Z[i, l, t])
            for c in model.C for i in model.I for l in model.L
        )

    # Reverse logistics
    total_zn_rev = 0.0
    for b in model.B:
        for t in model.T:
            wp  = pyo.value(model.W_proc[b, t]) or 0.0
            rzn = pyo.value(model.R_zn[b, t])   or 0.0
            if wp > 0.01:
                revenue = pyo.value(model.rev_zn) * rzn
                total_zn_rev += revenue
                kpis["zn_recovered"][t] = kpis["zn_recovered"].get(t, 0.0) + rzn
    kpis["total_zn_revenue"] = total_zn_rev

    total_disp = 0.0
    for k in model.K_smelter:
        for t in model.T:
            wd = pyo.value(model.W_disp[k, t]) or 0.0
            if wd > 0.01:
                total_disp += wd
    kpis["total_disposal"] = total_disp

    # Recovery plants opened
    built_rec = [b for b in model.B if pyo.value(model.XR[b]) > 0.5]
    kpis["recovery_plants_opened"] = len(built_rec)

    
    # One-line summary (always printed)
    
    gap_str = "{:.4f}%".format(gap * 100) if gap is not None else "N/A"
    print("  >> {:30s}  Obj=${:>15,.2f}  Gap={:>8s}  Shortage={:,.1f} t  "
          "Plants={}  ZnRev=${:,.0f}".format(
          label, obj_val, gap_str, total_shortage,
          kpis["recovery_plants_opened"], total_zn_rev))

    
    # Detailed printout (only when verbose=True)
    
    if verbose:
        # Banner
        print("\n" + sep)
        if "maxtimeLimit".lower() in status_str or "timelimit" in status_str:
            print("SOLUTION SUMMARY  [" + label + "]  ** TIME LIMIT REACHED (600 s) **")
        else:
            print("SOLUTION SUMMARY  [" + label + "]")
        print(sep)

        if gap is not None:
            gap_pct = gap * 100
            gap_tag = "  (target met)" if gap <= 0.005 else "  *** above 0.5% target ***"
            print("Optimality Gap : {:.4f}%{}".format(gap_pct, gap_tag))
        else:
            print("Optimality Gap : N/A")
        if "maxtimeLimit".lower() in status_str or "timelimit" in status_str:
            print("Stop reason    : Time limit (600 s) — best feasible solution loaded")

        # Objective
        print("\nTotal Cost (Objective): ${:,.2f}".format(obj_val))
        if gap is not None:
            print("Optimality Gap        : {:.4f}%{}".format(
                gap * 100,
                "  (target met)" if gap <= 0.005 else "  *** above 0.5% target ***"
            ))

        # Open warehouses
        print("\n--- Open Warehouses ---")
        open_wh = [j for j in model.J if pyo.value(model.X[j]) > 0.5]
        print("  " + str(open_wh))

        # Mine capacity levels
        print("\n--- Mine Capacity Levels (per period) ---")
        for t in model.T:
            print("  Period {}:".format(t))
            for i in model.I:
                for l in model.L:
                    if pyo.value(model.Z[i, l, t]) > 0.5:
                        print("    Mine {}: {}".format(i, l))

        # Production
        print("\n--- Production V (tonnes, non-zero only) ---")
        for t in model.T:
            for i in model.I:
                for p in model.P:
                    val = pyo.value(model.V[i, p, t])
                    if val is not None and val > 0.01:
                        print("  t={}  mine={}  product={}: {:,.1f} t".format(t, i, p, val))

        # Unmet demand
        print("\n--- Unmet Demand U (tonnes, non-zero only) ---")
        for t in model.T:
            for k in model.K:
                for p in model.P:
                    val = pyo.value(model.U[k, p, t])
                    if val is not None and val > 0.01:
                        print("  t={}  centre={}  product={}: {:,.1f} t".format(t, k, p, val))
        print("  Total unmet demand: {:,.1f} t".format(total_shortage))

        # Renewable installations
        print("\n--- Renewable Installations (Y = 1) ---")
        for i in model.I:
            for r in model.R:
                for o in model.O:
                    if pyo.value(model.Y[i, r, o]) > 0.5:
                        print("  Mine {}: {} at {} kW".format(i, r, o))

        # Energy mix
        print("\n--- Energy Mix per Period ---")
        for t in model.T:
            ren   = sum(pyo.value(model.Q[i, r, t]) for i in model.I for r in model.R)
            conv  = sum(pyo.value(model.S[i, t])    for i in model.I)
            total_e = ren + conv
            share = (ren / total_e * 100) if total_e > 0 else 0.0
            print("  t={}  Renewable: {:,.0f} kWh  Conventional: {:,.0f} kWh  Share: {:.1f}%"
                  .format(t, ren, conv, share))

        # Transport modes
        print("\n--- Transport Mode Usage (W = 1) ---")
        for t in model.T:
            for j in model.J:
                for mode in model.M:
                    if pyo.value(model.W[j, mode, t]) > 0.5:
                        print("  t={}  warehouse={}  mode={}".format(t, j, mode))

        # Environmental cost
        print("\n--- Environmental Cost per Period (UB_E = ${:,.2f}) ---".format(ub_e))
        for t in model.T:
            ec = kpis["env_costs"][t]
            binding = "  << BINDING" if abs(ec - ub_e) < 0.01 * ub_e else ""
            print("  t={}  Env Cost: ${:,.2f}{}".format(t, ec, binding))

        # Social scores
        print("\n--- Weighted Social Score per Period (LB_S = {:.4f}) ---".format(lb_s))
        for t in model.T:
            score = kpis["social_scores"][t]
            binding = "  << AT LOWER BOUND" if abs(score - lb_s) < 1e-4 else ""
            print("  t={}  Weighted Social Score: {:.4f}{}".format(t, score, binding))

        # Reverse logistics
        print("\n--- Reverse Logistics Summary ---")
        print("  Built recovery facilities: " + (str(built_rec) if built_rec else "None"))
        print("\n  Processing & Zinc Recovery (non-zero only):")
        for b in model.B:
            for t in model.T:
                wp  = pyo.value(model.W_proc[b, t]) or 0.0
                rzn = pyo.value(model.R_zn[b, t])   or 0.0
                if wp > 0.01:
                    revenue = pyo.value(model.rev_zn) * rzn
                    print("    b={}  t={}  Processed: {:,.1f} t  Zn: {:,.2f} t  Revenue: ${:,.0f}".format(
                        b, t, wp, rzn, revenue))
        print("  Total zinc revenue: ${:,.0f}".format(total_zn_rev))

        print("\n  Jarosite Disposed (non-zero only):")
        for k in model.K_smelter:
            for t in model.T:
                wd = pyo.value(model.W_disp[k, t]) or 0.0
                if wd > 0.01:
                    dcost = (pyo.value(model.cost_disp) + pyo.value(model.env_disp)) * wd
                    print("    k={}  t={}  Disposed: {:,.1f} t  Cost: ${:,.0f}".format(
                        k, t, wd, dcost))
        print("  Total disposed: {:,.1f} t".format(total_disp))

        print("\n" + sep)

    return kpis



# ENTRY POINT (sensitivity analysis)


if __name__ == "__main__":

    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

    
    # BASE RUN (verbose — full printout)
    
    print("\n" + "=" * 60)
    print("BASE RUN")
    print("=" * 60)
    m = build_model()
    base_kpis = solve_and_report(m, label="BASE", verbose=True)

    
    # SENSITIVITY ANALYSIS — 6 parameters × 5 values = 30 runs
    

    # Each entry: (display_name, attr_name, list_of_values, base_value)
    sensitivity_params = [
        ("alpha",    "alpha",    [0.0, 0.15, 0.30, 0.45, 0.60],        0.30),
        ("beta_min", "beta_min", [0.0, 0.10, 0.20, 0.40, 0.60],        0.20),
        ("UB_E",     "UB_E",     [450000, 500000, 550000, 600000, 650000], 550000),
        ("LB_S",     "LB_S",     [320, 325, 330, 335, 340],             330),
        ("rev_zn",   "rev_zn",   [1700, 1900, 2200, 2500, 2800],        2200),
        ("env_disp", "env_disp", [0, 25, 50, 75, 100],                  50),
    ]

    all_kpis   = []   # flat list of every kpis dict
    param_tags = []   # parallel list: which parameter each run belongs to

    total_runs = sum(len(vals) for _, _, vals, _ in sensitivity_params)
    run_count  = 0

    for param_name, attr_name, values, base_val in sensitivity_params:
        print("\n" + "=" * 60)
        print(f"SENSITIVITY: {param_name}")
        print("=" * 60)

        param_objs  = []   # objective values for chart (None if infeasible)
        param_vals  = []   # x-axis values for chart
        base_obj    = None

        for val in values:
            run_count += 1
            label = f"{param_name}={val}"
            print(f"\n[Run {run_count}/{total_runs}]  {label}")

            m = build_model()
            getattr(m, attr_name).set_value(val)
            kpis = solve_and_report(m, label=label, verbose=False)

            # Tag which parameter this run belongs to and the tested value
            kpis["_param"]      = param_name
            kpis["_param_val"]  = val
            kpis["_base_val"]   = base_val
            all_kpis.append(kpis)
            param_tags.append(param_name)

            if kpis["obj"] is None:
                print(f"  !! Run {run_count}/{total_runs} — INFEASIBLE, skipping.")
                param_objs.append(None)
            else:
                param_objs.append(kpis["obj"])
                if val == base_val:
                    base_obj = kpis["obj"]

            print(f"  Run {run_count}/{total_runs} done.")

        
        # Chart for this parameter
        
        fig, ax = plt.subplots(figsize=(7, 4))
        xs_plot = [v for v, o in zip(values, param_objs) if o is not None]
        ys_plot = [o for o in param_objs if o is not None]
        ax.plot(xs_plot, ys_plot, marker="o", color="steelblue",
                linewidth=1.8, markersize=6, label="Objective")

        # Highlight base-case point in red
        if base_obj is not None and base_val in xs_plot:
            ax.scatter([base_val], [base_obj], color="red", zorder=5,
                       s=80, label=f"Base ({base_val})")

        ax.set_title(f"Sensitivity: {param_name}", fontsize=13)
        ax.set_xlabel(param_name, fontsize=11)
        ax.set_ylabel("Objective ($)", fontsize=11)
        ax.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
        ax.legend(fontsize=9)
        ax.grid(True, linestyle="--", alpha=0.5)
        plt.tight_layout()

        chart_path = os.path.join(SCRIPT_DIR, f"sensitivity_{param_name}.png")
        fig.savefig(chart_path, dpi=150)
        plt.close(fig)
        print(f"  Chart saved: {chart_path}")

    
    # SUMMARY TABLE — CSV + console
    
    print("\n" + "=" * 60)
    print("SENSITIVITY RESULTS SUMMARY")
    print("=" * 60)

    rows = []
    for k in all_kpis:
        total_zn_rec = sum(k["zn_recovered"].values()) if k["zn_recovered"] else 0.0
        min_social   = min(k["social_scores"].values()) if k["social_scores"] else None
        gap_pct      = (k["gap"] * 100) if k["gap"] is not None else None
        rows.append({
            "Parameter":              k["_param"],
            "Value":                  k["_param_val"],
            "Status":                 k["status"],
            "Objective ($)":          k["obj"],
            "Gap (%)":                round(gap_pct, 4) if gap_pct is not None else None,
            "Total Unmet Demand (t)": k["shortage"],
            "Total Zn Recovered (t)": round(total_zn_rec, 2),
            "Total Zn Revenue ($)":   round(k["total_zn_revenue"], 2),
            "Total Disposed (t)":     k["total_disposal"],
            "Min Social Score":       round(min_social, 4) if min_social is not None else None,
            "Recovery Plants":        k["recovery_plants_opened"],
        })

    df = pd.DataFrame(rows)

    # Console print
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", "{:,.4f}".format)
    print(df.to_string(index=False))

    # CSV
    csv_path = os.path.join(SCRIPT_DIR, "sensitivity_results.csv")
    df.to_csv(csv_path, index=False)
    print(f"\nCSV saved: {csv_path}")
    print("All done.")
