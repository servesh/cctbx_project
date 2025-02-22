from __future__ import absolute_import, division, print_function
from cctbx import crystal
from cctbx import sgtbx
from cctbx import xray
from cctbx.array_family import flex
from libtbx.utils import Sorry, multi_out
import iotbx.phil
from iotbx import reflection_file_utils
from iotbx import crystal_symmetry_from_any
from iotbx.pdb import xray_structure
from iotbx import pdb
import mmtbx.scaling
from mmtbx.scaling import twin_analyses
from mmtbx import f_model
from mmtbx.twinning import twin_f_model
from six.moves import cStringIO as StringIO
import sys, os
from iotbx import extract_xtal_data

master_params = iotbx.phil.parse("""\
twin_utils{
  input{
    unit_cell=None
    .type=unit_cell
    space_group=None
    .type=space_group
    xray_data{
      file_name=None
      .type=path
      obs_labels=None
      .type=str
      free_flag=None
      .type=str
    }
    model{
      file_name=None
      .type=path
    }
  }
  parameters{
    twinning{
      twin_law=None
      .type=str
      max_delta=3.0
      .type=float
      detwin_mode=*algebraic proportional
      .type=choice
    }
  }
  output{
    logfile=twin_tools.log
    .type=str
    map_coeffs_root=MAP_COEFFS
    .type=str
    obs_and_calc="obs_and_calc.mtz"
    .type=str
  }
}
""")

def print_help(command_name):
  command_under = "-" * len(command_name)
  print("""
%(command_name)s
%(command_under)s

A command line utility to compute map coefficients for twinned data.
Bulk solvent parameters and twin fractions are determined automatically.
If no twin law is specified, map coefficents for all twin laws will
be computed.

The keywords are sumarized below:

twin_utils{
  input{
    unit_cell=None
    space_group=None
    xray_data{
      file_name=None
      obs_labels=None
      free_flag=None
      }
    model{
      file_name=None
      }
  }
  parameters{
    twinning{
      twin_law=None
      max_delta=3.0
    }
  }
  output{
    logfile=twin_tools.log
    map_coeffs_root=MAP_COEFFS
  }
}

A typical run looks like this:

%(command_name)s data.file=mydata.mtz model.file=mymodel.pdb \\
  obs_labels=FP,SIGFP free_flag=TEST

If no unit cell is specified, the unit cell of the reflection
file or the model will be used.
""" % vars())

def run(args, command_name="phenix.twin_map_utils"):
  log=sys.stdout
  params=None
  if (len(args) == 0 or "--help" in args or "--h" in args or "-h" in args):
    print_help(command_name=command_name)
  else:
    log = multi_out()
    if (not "--quiet" in args):
      log.register(label="stdout", file_object=sys.stdout)
    string_buffer = StringIO()
    string_buffer_plots = StringIO()
    log.register(label="log_buffer", file_object=string_buffer)

    phil_objects = []
    argument_interpreter = master_params.command_line_argument_interpreter(
      home_scope="map_coefs")

    for arg in args:
      command_line_params = None
      arg_is_processed = False
      # is it a file?
      if (os.path.isfile(arg)): ## is this a file name?
        # check if it is a phil file
        try:
          command_line_params = iotbx.phil.parse(file_name=arg)
          if command_line_params is not None:
            phil_objects.append(command_line_params)
            arg_is_processed = True
        except KeyboardInterrupt: raise
        except Exception : pass
      else:
        try:
          command_line_params = argument_interpreter.process(arg=arg)
          if command_line_params is not None:
            phil_objects.append(command_line_params)
            arg_is_processed = True
        except KeyboardInterrupt: raise
        except Exception : pass

      if not arg_is_processed:
        print("##----------------------------------------------##", file=log)
        print("## Unknown file or keyword:", arg, file=log)
        print("##----------------------------------------------##", file=log)
        print(file=log)
        raise Sorry("Unknown file or keyword: %s" % arg)

    effective_params = master_params.fetch(sources=phil_objects)
    params = effective_params.extract()
    """
    new_params =  master_params.format(python_object=params)
    new_params.show(out=log)
    """
    # now get the unit cell from the pdb file

    hkl_xs = crystal_symmetry_from_any.extract_from(
      file_name=params.twin_utils.input.xray_data.file_name)
    pdb_xs = crystal_symmetry_from_any.extract_from(
      file_name=params.twin_utils.input.model.file_name)

    phil_xs=None
    if ([params.twin_utils.input.unit_cell,
         params.twin_utils.input.space_group]).count(None)<2:
      phil_xs = crystal.symmetry(
        unit_cell=params.twin_utils.input.unit_cell,
        space_group_info=params.twin_utils.input.space_group  )


    combined_xs = crystal.select_crystal_symmetry(
      None,phil_xs, [pdb_xs],[hkl_xs])

    # inject the unit cell and symmetry in the phil scope please
    params.twin_utils.input.unit_cell = combined_xs.unit_cell()
    params.twin_utils.input.space_group = \
      sgtbx.space_group_info( group = combined_xs.space_group() )

    new_params =  master_params.format(python_object=params)
    new_params.show(out=log)

    if params.twin_utils.input.unit_cell is None:
      raise Sorry("unit cell not specified")
    if params.twin_utils.input.space_group is None:
      raise Sorry("space group not specified")
    if params.twin_utils.input.xray_data.file_name is None:
      raise Sorry("Xray data not specified")
    if params.twin_utils.input.model.file_name is None:
      raise Sorry("pdb file with  model not specified")

    #-----------------------------------------------------------
    #
    # step 1: read in the reflection file
    #
    phil_xs = crystal.symmetry(
      unit_cell=params.twin_utils.input.unit_cell,
      space_group_info=params.twin_utils.input.space_group  )

    xray_data_server =  reflection_file_utils.reflection_file_server(
      crystal_symmetry = phil_xs,
      force_symmetry = True,
      reflection_files=[])

    miller_array = None
    free_flags = None

    tmp_params = extract_xtal_data.data_and_flags_master_params().extract()
    # insert proper values please
    tmp_params.file_name = params.twin_utils.input.xray_data.file_name
    tmp_params.labels = params.twin_utils.input.xray_data.obs_labels
    tmp_params.r_free_flags.file_name=params.twin_utils.input.xray_data.file_name
    tmp_params.r_free_flags.label=params.twin_utils.input.xray_data.free_flag

    tmp_object = extract_xtal_data.run(
      reflection_file_server = xray_data_server)

    miller_array = tmp_object.extract_data()
    if miller_array.is_xray_intensity_array():
      miller_array = miller_array.f_sq_as_f()
    assert miller_array.is_xray_amplitude_array()

    free_flags = tmp_object.extract_flags(data = miller_array)
    print(file=log)
    print("Attempting to extract Free R flags", file=log)

    free_flags = free_flags.customized_copy( data = flex.bool( free_flags.data()==1 ) )
    if free_flags is None:
      free_flags = miller_array.generate_r_free_flags(use_lattice_symmetry=True)

    assert miller_array.observation_type() is not None

    print(file=log)
    print("Summary info of observed data", file=log)
    print("=============================", file=log)
    miller_array.show_summary(f=log)
    print(file=log)

    if miller_array.indices().size() == 0:
      raise Sorry("No data available")

    #----------------------------------------------------------------
    # Step 2: get an xray structure from the PDB file
    #
    model = pdb.input(file_name=params.twin_utils.input.model.file_name).xray_structure_simple(
      crystal_symmetry=phil_xs)
    print("Atomic model summary", file=log)
    print("====================", file=log)
    model.show_summary(f=log)
    print(file=log)


    #----------------------------------------------------------------
    # step 3: get the twin laws for this xs
    twin_laws = twin_analyses.twin_laws(
      miller_array,
      lattice_symmetry_max_delta=\
         params.twin_utils.parameters.twinning.max_delta,
      out=log)

    print(file=log)
    print("Preliminary data analyses", file=log)
    print("==========================", file=log)
    twin_laws.show(out=log)


    #---------
    # step 3:
    # make twin model managers for all twin laws
    print(file=log)
    print("Overall and bulk solvent scale paranmeters and twin fraction estimation", file=log)
    print("=======================================================================", file=log)
    twin_models = []
    operator_count = 0

    if params.twin_utils.parameters.twinning.twin_law is not None:
      tmp_law = sgtbx.rt_mx( params.twin_utils.parameters.twinning.twin_law )
      tmp_law = twin_analyses.twin_law(tmp_law,None,None,None,None,None)
      twin_laws.operators = [ tmp_law ]
    for twin_law in twin_laws.operators:
      operator_count += 1
      operator_hkl = sgtbx.change_of_basis_op( twin_law.operator ).as_hkl()
      twin_model = twin_f_model.twin_model_manager(
        f_obs=miller_array,
        r_free_flags = free_flags,
        xray_structure=model,
        twin_law = twin_law.operator,
        detwin_mode = params.twin_utils.parameters.twinning.detwin_mode,
        out=log)


      print("--- bulk solvent scaling ---", file=log)
      twin_model.update_all_scales()
      twin_model.r_values()
      twin_model.target()
      twin_model.show_k_sol_b_sol_b_cart_target()
      twin_model.show_essential()

      wfofc  = twin_model.map_coefficients(map_type="mFo-DFc"  )
      wtfofc = twin_model.map_coefficients(map_type="2mFo-DFc" )
      grad   = twin_model.map_coefficients(map_type="gradient"       )

      mtz_dataset = wtfofc.as_mtz_dataset(
        column_root_label="FWT")
      mtz_dataset = mtz_dataset.add_miller_array(
        miller_array = wfofc,
        column_root_label = "DFWT"
        )
      mtz_dataset = mtz_dataset.add_miller_array(
        miller_array = grad,
        column_root_label = "GRAD"
        )
      name = params.twin_utils.output.map_coeffs_root+"_"+str(operator_count)+".mtz"
      print(file=log)
      print("Writing %s for twin law %s"%(name,operator_hkl), file=log)
      print(file=log)
      mtz_dataset.mtz_object().write(
        file_name=name)

      if params.twin_utils.output.obs_and_calc is not None:
        # i want also a Fobs and Fmodel combined dataset please
        mtz_dataset = miller_array.as_mtz_dataset(
          column_root_label="FOBS")
        mtz_dataset = mtz_dataset.add_miller_array(
          miller_array = twin_model.f_model(),
          column_root_label="FMODEL")
        name = params.twin_utils.output.obs_and_calc
        mtz_dataset.mtz_object().write(
          file_name=name)

    if len(twin_laws.operators)==0:
      print(file=log)
      print("No twin laws were found", file=log)
      print("Performing maximum likelihood based bulk solvent scaling", file=log)
      f_model_object = f_model.manager(
        f_obs = miller_array,
        r_free_flags = free_flags,
        xray_structure = model )
      f_model_object.update_all_scales(log=log)
      tfofc =  f_model_object.map_coefficients(map_type="2mFobs-DFmodel")
      fofc = f_model_object.map_coefficients(map_type="mFobs-DFmodel")
      mtz_dataset = tfofc.as_mtz_dataset(
        column_root_label="FWT")
      mtz_dataset = mtz_dataset.add_miller_array(
        miller_array = fofc,
        column_root_label = "DELFWT"
      )
      name = params.twin_utils.output.map_coeffs_root+"_ML.mtz"
      mtz_dataset.mtz_object().write(
        file_name=name)

      if params.twin_utils.output.obs_and_calc is not None:
        # i want also a Fobs and Fmodel combined dataset please
        mtz_dataset = miller_array.as_mtz_dataset(
          column_root_label="FOBS")
        mtz_dataset = mtz_dataset.add_miller_array(
          miller_array = f_model_object.f_model(),
          column_root_label="FMODEL")
        name = params.twin_utils.output.obs_and_calc
        mtz_dataset.mtz_object().write(
          file_name=name)

    print(file=log)
    print(file=log)
    print("All done \n", file=log)
    logfile = open(params.twin_utils.output.logfile,'w')
    print(string_buffer.getvalue(), file=logfile)
    print(file=log)
