#! /usr/bin/env python
# encoding: utf-8

"""Workflow runner, interfacing to external workflow engine
using Abstract Beamline Interface messages

License:

This file is part of MXCuBE.

MXCuBE is free software: you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

MXCuBE is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License
along with MXCuBE. If not, see <https://www.gnu.org/licenses/>.
"""
from __future__ import division, absolute_import
from __future__ import print_function, unicode_literals

import copy
import logging
import enum
import time
import datetime
import os
import math
import subprocess
import socket
from collections import OrderedDict

import gevent
import gevent.event
import gevent.queue
import f90nml

from mxcubecore.dispatcher import dispatcher
from mxcubecore.utils import conversion, ui_communication
from mxcubecore.BaseHardwareObjects import HardwareObjectYaml
from mxcubecore.model import queue_model_objects
from mxcubecore.model import queue_model_enumerables
from mxcubecore.queue_entry import QUEUE_ENTRY_STATUS
from mxcubecore.queue_entry import QueueAbortedException

from mxcubecore.HardwareObjects.Gphl import GphlMessages

from mxcubecore import HardwareRepository as HWR

@enum.unique
class GphlWorkflowStates(enum.Enum):
    """
    BUSY = "Workflow is executing"
    READY = "Workflow is idle and ready to start"
    FAULT = "Workflow shutting down from an error"
    ABORTED = "HWorkflow shutting down after an abort or stop command
    COMPLETED = "Workflow has finished successfully"
    UNKNOWN = "Workflow state unknown"
    """

    BUSY = 0
    READY = 1
    FAULT = 2
    ABORTED = 3
    COMPLETED = 4
    UNKNOWN = 5

__copyright__ = """ Copyright © 2016 - 2019 by Global Phasing Ltd. """
__license__ = "LGPLv3+"
__author__ = "Rasmus H Fogh"

# Additional sample/diffraction plan data for GPhL emulation samples.
EMULATION_DATA = {
    "3n0s": {"radiationSensitivity": 0.9},
    "4j8p": {"radiationSensitivity": 1.1},
}

# Centring modes for use in centring mode pulldown.
# The dictionary keys are labels (changeable),
# the values are passed to the program (not changeable)
# The first value is the default
RECENTRING_MODES = OrderedDict(
    (
        ("Re-centre when orientation changes", "sweep"),
        ("Re-centre at the start of each wedge", "scan"),
        ("Re-centre all before acquisition start", "start"),
        ("No manual re-centring, rely on calculated values", "none"),
    )
)


class GphlWorkflow(HardwareObjectYaml):
    """Global Phasing workflow runner.
    """
    SPECIFIC_STATES = GphlWorkflowStates

    TEST_SAMPLE_PREFIX = "emulate"

    def __init__(self, name):
        super(GphlWorkflow, self).__init__(name)

        # Needed to allow methods to put new actions on the queue
        # And as a place to get hold of other objects
        self._queue_entry = None

        # Configuration data - set on load
        self.workflows = OrderedDict()
        self.settings = {}
        self.test_crystals = {}
        # auxiliary data structure from configuration. Set in init
        self.workflow_strategies = OrderedDict()

        # Current data collection task group. Different for characterisation and collection
        self._data_collection_group = None

        # event to handle waiting for parameter input
        self._return_parameters = None

        # Queue to read messages from GphlConnection
        self._workflow_queue = None

        # Message - processing function map
        self._processor_functions = {}

        # Subprocess names to track which subprocess is getting info
        self._server_subprocess_names = {}

        # Rotation axis role names, ordered from holder towards sample
        self.rotation_axis_roles = []

        # Translation axis role names
        self.translation_axis_roles = []

        # Switch for 'move-to-fine-zoom' message for translational calibration
        self._use_fine_zoom = False

        # Configurable file paths
        self.file_paths = {}

        # TEST mxcube3 UI
        self.gevent_event = gevent.event.Event()
        self.params_dict = {}

    def _init(self):
        super(GphlWorkflow, self)._init()

    def init(self):
        super(GphlWorkflow, self).init()

        # Set up processing functions map
        self._processor_functions = {
            "String": self.echo_info_string,
            "SubprocessStarted": self.echo_subprocess_started,
            "SubprocessStopped": self.echo_subprocess_stopped,
            "RequestConfiguration": self.get_configuration_data,
            "GeometricStrategy": self.setup_data_collection,
            "CollectionProposal": self.collect_data,
            "ChooseLattice": self.select_lattice,
            "RequestCentring": self.process_centring_request,
            "PrepareForCentring": self.prepare_for_centring,
            "ObtainPriorInformation": self.obtain_prior_information,
            "WorkflowAborted": self.workflow_aborted,
            "WorkflowCompleted": self.workflow_completed,
            "WorkflowFailed": self.workflow_failed,
        }

        # Set standard configurable file paths
        file_paths = self.file_paths
        ss0 = HWR.beamline.gphl_connection.software_paths["gphl_beamline_config"]
        file_paths["gphl_beamline_config"] = ss0
        file_paths["transcal_file"] = os.path.join(ss0, "transcal.nml")
        file_paths["diffractcal_file"] = os.path.join(ss0, "diffractcal.nml")
        file_paths["instrumentation_file"] = fp0 = os.path.join(
            ss0, "instrumentation.nml"
        )
        instrument_data = f90nml.read(fp0)["sdcp_instrument_list"]
        self.rotation_axis_roles = instrument_data["gonio_axis_names"]
        self.translation_axis_roles = instrument_data["gonio_centring_axis_names"]

        detector = HWR.beamline.detector
        if "Mockup" in detector.__class__.__name__:
            # We are in mock  mode
            # - set detector centre to match instrumentaiton.nml
            # NB this sould be done with isinstance, but that seems to fail,
            # probably because of import path mix-ups.
            detector._set_beam_centre(
                (instrument_data["det_org_x"], instrument_data["det_org_y"])
            )

        # Adapt configuration data - must be done after file_paths setting
        if HWR.beamline.gphl_connection.ssh_options:
            # We are running workflow through ssh - set beamline url
            beamline_hook ="py4j:%s:" % socket.gethostname()
        else:
            beamline_hook = "py4j::"

        # Consolidate workflow options
        for title, workflow in self.workflows.items():
            workflow["wfname"] = title
            wftype = workflow["wftype"]

            opt0 = workflow.get("options", {})
            opt0["beamline"] = beamline_hook
            default_strategy_name = None
            for strategy in workflow["strategies"]:
                strategy_name = strategy["title"]
                self.workflow_strategies[strategy_name] = strategy
                strategy["wftype"] = wftype
                if default_strategy_name is None:
                    default_strategy_name = workflow["strategy_name"] = strategy_name
                dd0 = opt0.copy()
                dd0.update(strategy.get("options", {}))
                strategy["options"] = dd0
                if workflow["wftype"] == "transcal":
                    relative_file_path = strategy["options"].get("file")
                    if relative_file_path is not None:
                        # Special case - this option must be modified before use
                        strategy["options"]["file"] = os.path.join(
                            self.file_paths["gphl_beamline_config"], relative_file_path
                        )

        self.update_state(self.STATES.READY)

    def shutdown(self):
        """Shut down workflow and connection. Triggered on program quit."""
        workflow_connection = HWR.beamline.gphl_connection
        if workflow_connection is not None:
            workflow_connection.workflow_ended()
            workflow_connection.close_connection()

    def get_available_workflows(self):
        """Get list of workflow description dictionaries."""
        return copy.deepcopy(self.workflows)

    def query_pre_strategy_params(self, choose_lattice=None):
        """Query pre_strategy parameters.
        Used for both characterisation, diffractcal, and acquisition

        :param data_model (GphlWorkflow): GphlWorkflow QueueModelObjecy
        :param choose_lattice (ChooseLattice): GphlMessage.ChooseLattice
        :return: -> dict
        """
        data_model = self._queue_entry.get_data_model()
        workflow_parameters = data_model.get_workflow_parameters()
        schema = {
            "title": "GΦL Pre-strategy parameters",
            "type": "object",
            "properties": {},
            "definitions": {},
        }
        fields = schema["properties"]
        fields["cell_a"] = {
            "title": "A",
            "type": "number",
            "minimum": 0,
        }
        fields["cell_b"] = {
            "title": "B",
            "type": "number",
            "minimum": 0,
        }
        fields["cell_c"] = {
            "title": "C",
            "type": "number",
            "minimum": 0,
        }
        fields["cell_alpha"] = {
            "title": "α",
            "type": "number",
            "minimum": 0,
            "maximum": 180,
        }
        fields["cell_beta"] = {
            "title": "β",
            "type": "number",
            "minimum": 0,
            "maximum": 180,
        }
        fields["cell_gamma"] = {
            "title": "γ",
            "type": "number",
            "minimum": 0,
            "maximum": 180,
        }
        fields[ "crystal_family"] = {
            "title": "Prior crystal family",
            "default": data_model.crystal_family or "",
            "$ref": "#/definitions/crystal_family",
        }
        fields["point_group"] = {
            "title": "Point Group",
            "default": data_model.point_group or "",
            "$ref": "#/definitions/point_group",
        }
        fields["space_group"] = {
            "title": "Space Group",
            "default": data_model.space_group or "",
            "$ref": "#/definitions/space_group",
        }
        fields["relative_rad_sensitivity"] = {
            "title": "Radiation sensitivity",
            "default": data_model.relative_rad_sensitivity or 1.0,
            "type": "number",
            "minimum": 0,
        }
        fields["use_cell_for_processing"] = {
            "title": "Use for indexing",
            "type": "boolean",
            "default": False,
        }
        fields["resolution"] = {
            "title": "Resolution",
            "type": "number",
            "default": HWR.beamline.resolution.get_value(),
            "minimum": 0,
        }
        fields["decay_limit"] = {
            "title": "Signal decay limit (%)",
            "type": "number",
            "default": 25,
            "minimum": 1,
            "maximum": 99,
        }
        fields["strategy"] = {
            "title": "Strategy",
            "$ref": "#/definitions/strategy",

        }
        schema["definitions"]["crystal_family"] = list(
            {
                "type": "string",
                "enum": [tag],
                "title": tag,
            }
            for tag in queue_model_enumerables.lattice2xtal_point_groups
        )
        schema["definitions"]["point_group"] = list(
            {
                "type": "string",
                "enum": [tag],
                "title": tag,
            }
            for tag in queue_model_enumerables.xtal_point_groups
        )
        schema["definitions"]["space_group"] = list(
            {
                "type": "string",
                "enum": [tag],
                "title": tag,
            }
            for tag in queue_model_enumerables.XTAL_SPACEGROUPS
        )
        # Handle strategy fields
        if data_model.characterisation_done or data_model.get_type() == "diffractcal":
            strategies = workflow_parameters["variants"]
            fields["strategy"]["default"] = strategies[0]
            fields["strategy"]["title"] = "Acquisition strategy"
            ll0 = workflow_parameters.get("beam_energy_tags")
            if ll0:
                energy_tag = ll0[0]
            else:
                energy_tag = self.settings["default_beam_energy_tag"]
        else:
            # Characterisation
            strategies = self.settings["characterisation_strategies"]
            fields["strategy"]["default"] = (
                self.settings["defaults"]["characterisation_strategy"]
            )
            fields["strategy"]["title"] = "Characterisation strategy"
            energy_tag = "Characterisation"
        schema["definitions"]["strategy"] = list(
            {
                "type": "string",
                "enum": [tag],
                "title": tag,
            }
            for tag in strategies
        )
        # Handle cell parameters
        cell_parameters = data_model.cell_parameters
        if cell_parameters:
            for tag, val in zip(
                ("cell_a", "cell_b", "cell_c", "cell_alpha", "cell_beta", "cell_gamma"),
                data_model.cell_parameters
            ):
                fields[tag]["default"] = val

        ui_schema = {
            "ui:order": ["crystal_data", "parameters"],
            "ui:widget": "vertical_box",
            "crystal_data": {
                "ui:title": "Unit Cell",
                "ui:widget": "column_grid",
                "ui:order": [ "symmetry", "cell_edges", "cell_angles",],
                "cell_edges": {
                    "ui:order": ["cell_a", "cell_b", "cell_c"],
                },
                "cell_angles": {
                    "ui:order": ["cell_alpha", "cell_beta", "cell_gamma"],
                },
                "symmetry": {
                    "ui:order": [
                        "crystal_family",
                        "point_group",
                        "space_group",
                        "use_cell_for_processing",
                    ],
                },
            },
            "parameters": {
                "ui:title": "Parameters",
                "ui:widget": "column_grid",
                "ui:order": ["column1", "column2"],
                "column1": {
                    "ui:order": [
                        "relative_rad_sensitivity",
                        "strategy",
                        "decay_limit"
                    ],
                    "relative_rad_sensitivity": {
                        "ui:options": {
                            "decimals": 2,
                        }
                    },
                    # "strategy":{
                    #     "ui:widget": "select",
                    # },
                    "decay_limit": {
                        "ui:readonly": True,
                        "ui:options": {
                            "decimals": 1,
                        },
                    },
                },
                "column2": {
                    "ui:order": ["resolution",],
                    "resolution": {
                        "ui:options": {
                            "decimals": 3,
                        },
                    },
                },
            },
        }

        # Handle energy field
        # NBNB allow for fixed-energy beamlines
        energy_limits = HWR.beamline.energy.get_limits()
        col2 = ui_schema["parameters"]["column2"]
        tag = "energy"
        fields[tag] = {
            "title": "%s energy (keV)"  % energy_tag,
            "type": "number",
            "default": HWR.beamline.energy.get_value(),
            "minimum": energy_limits[0],
            "maximum": energy_limits[1],
        }
        col2["ui:order"].append(tag)
        col2[tag] = {
                    "ui:options": {
                        "decimals": 4,
                    },
        }

        if choose_lattice is not None:
            # Must match bravaisLattices column
            lattices = choose_lattice.lattices

            # First letter must match first letter of BravaisLattice
            crystal_family_char = choose_lattice.crystalFamilyChar

            header, soldict, select_row = self.parse_indexing_solution(choose_lattice)
            # header, solutions = self.parse_indexing_solution(
            #     solution_format, choose_lattice.solutions
            # )
            fields["indexing_solution"] = {
                "title": "Select indexing solution:",
                "type": "string",
            }

            # Color green (figuratively) if matches lattices,
            # or otherwise if matches crystalSystem
            colour_check = lattices or crystal_family_char or ()
            if colour_check:
                colouring = list(
                    any(x in solution.bravaisLattice for x in colour_check)
                    for solution in soldict.values()
                )
            else:
                colouring = None

            ui_schema["ui:order"].insert(0,"indexing_solution")
            ui_schema["indexing_solution"] = {
                "ui:widget": "selection_table",
                "ui:options": {
                    "header": [header],
                    "content": [list(soldict)],
                    "select_row": select_row,
                    "colouring": colouring,
                }
            }

        self._return_parameters = gevent.event.AsyncResult()

        try:
            responses = dispatcher.send(
                "gphlJsonParametersNeeded",
                self,
                schema,
                ui_schema,
                self._return_parameters,
            )
            if not responses:
                self._return_parameters.set_exception(
                    RuntimeError("Signal 'gphlJsonParametersNeeded' is not connected")
                )

            params = self._return_parameters.get()
            if params is StopIteration:
                return StopIteration
            solline = params.get("indexing_solution")
            if solline:
                params["indexing_solution"] = soldict[solline[0]]
        finally:
            self._return_parameters = None

        # Convert energy field to a single tuple
        params["energies"] = (params.pop("energy"),)
        #
        return params


    def pre_execute(self, queue_entry):
        if self.is_ready():
            self.update_state(self.STATES.BUSY)
        else:
            raise RuntimeError(
                "Cannot execute workflow - GphlWorkflow HardwareObject is not ready"
            )
        self._queue_entry = queue_entry
        data_model = queue_entry.get_data_model()
        self._workflow_queue = gevent.queue.Queue()
        HWR.beamline.gphl_connection.open_connection()
        if data_model.automation_mode:
            params = data_model.auto_acq_parameters[0]
        else:
            # SIGNAL TO GET Pre-strategy parameters here
            # NB set defaults from data_model
            # NB consider whether to override on None
            params = self.query_pre_strategy_params()
            if params is StopIteration:
                self.workflow_failed()
                return

        # print ('@~@~ pre_execute returned parameters :')
        # for item in sorted(params.items()):
        #     print (item)
        # print ('@~@~ end parameters\n\n')
        cell_tags = (
            "cell_a", "cell_b", "cell_c", "cell_alpha", "cell_beta", "cell_gamma"
        )
        cell_parameters = tuple(params.pop(tag, None) for tag in cell_tags)
        if None not in cell_parameters:
            params["cell_parameters"] = cell_parameters

        data_model.set_pre_strategy_params(**params)
        # @~@~ DEBUG:
        format = "--> %s: %s"
        print ("GPHL workflow. Pre-execute with parameters:")
        for item in data_model.parameter_summary().items():
            print (format % item)
        if data_model.detector_setting is None:
            resolution = HWR.beamline.resolution.get_value()
            distance = HWR.beamline.detector.distance.get_value()
            orgxy = HWR.beamline.detector.get_beam_position()
            data_model.detector_setting = GphlMessages.BcsDetectorSetting(
                resolution, orgxy=orgxy, Distance=distance
            )
        else:
            # Set detector distance and resolution
            distance = data_model.detector_setting.axisSettings["Distance"]
            HWR.beamline.detector.distance.set_value(distance, timeout=30)
        # self.test_dialog()

    def execute(self):

        if HWR.beamline.gphl_connection is None:
            raise RuntimeError(
                "Cannot execute workflow - GphlWorkflowConnection not found"
            )

        # Fork off workflow server process
        HWR.beamline.gphl_connection.start_workflow(
            self._workflow_queue, self._queue_entry.get_data_model()
        )

        while True:
            if self._workflow_queue is None:
                # We can only get that value if we have already done post_execute
                # but the mechanics of aborting means we conme back
                # Stop further processing here
                raise QueueAbortedException("Aborting...", self)

            tt0 = self._workflow_queue.get()
            if tt0 is StopIteration:
                logging.getLogger("HWR").debug("GPhL queue StopIteration")
                break

            message_type, payload, correlation_id, result_list = tt0
            func = self._processor_functions.get(message_type)
            if func is None:
                logging.getLogger("HWR").error(
                    "GPhL message %s not recognised by MXCuBE. Terminating...",
                    message_type,
                )
                break
            elif message_type != "String":
                logging.getLogger("HWR").info("GPhL queue processing %s", message_type)
                response = func(payload, correlation_id)
                if result_list is not None:
                    result_list.append((response, correlation_id))

    def post_execute(self):
        """
        The workflow has finished, sets the state to 'READY'
        """
        self.emit("gphl_workflow_finished", self.get_specific_state().name)
        self.update_state(self.STATES.READY)

        self._queue_entry = None
        self._data_collection_group = None
        self._server_subprocess_names.clear()
        self._workflow_queue = None
        if HWR.beamline.gphl_connection is not None:
            HWR.beamline.gphl_connection.workflow_ended()
            # HWR.beamline.gphl_connection.close_connection()

    def update_state(self, state=None):
        """
        Update state, resetting
        """
        super(GphlWorkflow, self).update_state(state=state)
        tag = self.get_state().name
        if tag in ("BUSY", "READY", "UNKNOWN", "FAULT"):
            self.update_specific_state(getattr(self.SPECIFIC_STATES, tag))

    def _add_to_queue(self, parent_model_obj, child_model_obj):
        HWR.beamline.queue_model.add_child(parent_model_obj, child_model_obj)

    # Message handlers:

    def workflow_aborted(self, payload=None, correlation_id=None):
        logging.getLogger("user_level_log").warning("GPhL Workflow aborted.")
        self.update_specific_state(self.SPECIFIC_STATES.ABORTED)
        self._workflow_queue.put_nowait(StopIteration)

    def workflow_completed(self, payload=None, correlation_id=None):
        logging.getLogger("user_level_log").info("GPhL Workflow completed.")
        self.update_specific_state(self.SPECIFIC_STATES.COMPLETED)
        self._workflow_queue.put_nowait(StopIteration)

    def workflow_failed(self, payload=None, correlation_id=None):
        logging.getLogger("user_level_log").warning("GPhL Workflow failed.")
        self.update_specific_state(self.SPECIFIC_STATES.FAULT)
        self._workflow_queue.put_nowait(StopIteration)

    def echo_info_string(self, payload, correlation_id=None):
        """Print text info to console,. log etc."""
        subprocess_name = self._server_subprocess_names.get(correlation_id)
        if subprocess_name:
            logging.info("%s: %s" % (subprocess_name, payload))
        else:
            logging.info(payload)

    def echo_subprocess_started(self, payload, correlation_id):
        name = payload.name
        if correlation_id:
            self._server_subprocess_names[correlation_id] = name
        logging.info("%s : STARTING", name)

    def echo_subprocess_stopped(self, payload, correlation_id):
        try:
            name = self._server_subprocess_names.pop(correlation_id)
        except KeyError:
            name = "Unknown process"
        logging.info("%s : FINISHED", name)

    def get_configuration_data(self, payload, correlation_id):
        return GphlMessages.ConfigurationData(self.file_paths["gphl_beamline_config"])

    def query_collection_strategy(self, geometric_strategy):
        """Display collection strategy for user approval,
        and query parameters needed"""

        data_model = self._queue_entry.get_data_model()
        workflow_parameters = data_model.get_workflow_parameters()

        # Number of decimals for rounding use_dose values
        use_dose_decimals = 4
        is_interleaved = geometric_strategy.isInterleaved

        data_model = self._queue_entry.get_data_model()
        budget_use_fraction = data_model.characterisation_budget_fraction
        initial_energy = HWR.beamline.energy.calculate_energy(
            data_model.wavelengths[0].wavelength
        )
        wf_parameters = data_model.get_workflow_parameters()

        orientations = OrderedDict()
        strategy_length = 0
        axis_setting_dicts = OrderedDict()
        for sweep in geometric_strategy.get_ordered_sweeps():
            strategy_length += sweep.width
            rotation_id = sweep.goniostatSweepSetting.id_
            if rotation_id in orientations:
                orientations[rotation_id].append(sweep)
            else:
                orientations[rotation_id] = [sweep]
                axis_settings = sweep.goniostatSweepSetting.axisSettings.copy()
                axis_settings.pop(sweep.goniostatSweepSetting.scanAxis, None)
                axis_setting_dicts[rotation_id] = axis_settings

        # Make info_text and do some setting up
        axis_names = self.rotation_axis_roles
        if (
            data_model.characterisation_done
            or wf_parameters.get("strategy_type") == "diffractcal"
        ):
            title_string = data_model.get_type()
            lines = [
                "GΦL workflow %s, strategy '%s'"
                % (title_string, data_model.strategy_options["variant"])
            ]
            lines.extend(("-" * len(lines[0]), ""))
            energy_tags = (
                workflow_parameters.get("beam_energy_tags")
                or (self.settings["default_beam_energy_tag"],)
            )
            beam_energies = OrderedDict()
            energies = [initial_energy, initial_energy + 0.01, initial_energy - 0.01]
            for ii, tag in enumerate(energy_tags):
                beam_energies[tag] = energies[ii]
            dose_label = "Dose/repetition (MGy)"

        else:
            # Characterisation
            title_string = "Characterisation"
            lines = ["GΦL Characterisation strategy"]
            lines.extend(("=" * len(lines[0]), ""))
            beam_energies = OrderedDict((("Characterisation", initial_energy),))
            dose_label = "Characterisation dose (MGy)"
            if not self.settings.get("recentre_before_start"):
                # replace planned orientation with current orientation
                current_pos_dict = HWR.beamline.diffractometer.get_positions()
                dd0 = list(axis_setting_dicts.values())[0]
                for tag in dd0:
                    pos = current_pos_dict.get(tag)
                    if pos is not None:
                        dd0[tag] = pos

        # Make strategy-description info_text
        if len(beam_energies) > 1:
            lines.append(
                "Experiment length (per repetition): %s * %6.1f°"
                % (len(beam_energies), strategy_length)
            )
        else:
            lines.append("Experiment length (per repetition): %6.1f°" % strategy_length)

        for rotation_id, sweeps in orientations.items():
            axis_settings = axis_setting_dicts[rotation_id]
            ss0 = "\nSweep :  " + ",  ".join(
                "%s= %6.1f°" % (x, axis_settings.get(x))
                for x in axis_names
                if x in axis_settings
            )
            ll1 = []
            for sweep in sweeps:
                start = sweep.start
                width = sweep.width
                ss1 = "%s= %6.1f°,  sweep width= %6.1f°" % (
                    sweep.goniostatSweepSetting.scanAxis, start, width
                )
                ll1.append(ss1)
            lines.append(ss0 + ",  " + ll1[0])
            spacer = " " * (len(ss0) + 2)
            for ss1 in ll1[1:]:
                lines.append(spacer + ss1)
        info_text = "\n".join(lines)

        # Set up image width pulldown
        allowed_widths = geometric_strategy.allowedWidths
        if allowed_widths:
            default_width_index = geometric_strategy.defaultWidthIdx or 0
        else:
            allowed_widths = list(self.settings.get("default_image_widths"))
            val = allowed_widths[0]
            allowed_widths.sort()
            default_width_index = allowed_widths.index(val)
            logging.getLogger("HWR").info(
                "No allowed image widths returned by strategy - use defaults"
            )

        # set starting and unchanging values of parameters
        acq_parameters = HWR.beamline.get_default_acquisition_parameters()

        resolution = HWR.beamline.resolution.get_value()
        dose_budget = self.resolution2dose_budget(
            resolution,
            decay_limit=data_model.decay_limit,
        )
        default_image_width = float(allowed_widths[default_width_index])
        default_exposure = acq_parameters.exp_time
        exposure_limits = HWR.beamline.detector.get_exposure_time_limits()
        total_strategy_length = strategy_length * len(beam_energies)
        data_model.strategy_length = total_strategy_length
        # NB - this is the default starting value, so repetition_count is 1 at this point
        experiment_time = total_strategy_length * default_exposure / default_image_width
        if (
            data_model.characterisation_done
            or wf_parameters.get("strategy_type") == "diffractcal"
        ):
            proposed_dose = dose_budget - data_model.characterisation_dose
        else:
            proposed_dose = (
                dose_budget * data_model.characterisation_budget_fraction
            )
        proposed_dose = round(max(proposed_dose,0), use_dose_decimals)

        # For calculating dose-budget transmission
        flux_density = HWR.beamline.flux.get_average_flux_density(transmission=100.0)
        if flux_density:
            std_dose_rate = (
                HWR.beamline.flux.get_dose_rate_per_photon_per_mmsq(initial_energy)
                * flux_density
                * 1.0e-6  # convert to MGy/s
            )
        else:
            std_dose_rate = 0
        transmission = acq_parameters.transmission

        reslimits = HWR.beamline.resolution.get_limits()
        if None in reslimits:
            reslimits = (0.5, 5.0)
        if std_dose_rate:
            use_dose_start = proposed_dose
            use_dose_frozen = False
        else:
            use_dose_start = 0
            use_dose_frozen = True
            logging.getLogger("user_level_log").warning(
                "Dose rate cannot be calculated - dose bookkeeping disabled"
            )

        schema = {
            "title": "GΦL %s parameters" % title_string,
            "type": "object",
            "properties": {},
            "definitions": {},
        }
        fields = schema["properties"]
        fields["total_strategy_length"] = {
            "title": "Strategy length",
            "type": "number",
            "default": total_strategy_length,
            "hidden": True,
        }
        fields["std_dose_rate"] = {
            "title": "Dose rate",
            "type": "number",
            "default": std_dose_rate,
            "hidden": True,
        }
        fields["decay_limit"] = {
            "title": "Decay limit",
            "type": "number",
            "default": data_model.decay_limit,
            "hidden": True,
        }
        fields["relative_rad_sensitivity"] = {
            "title": "Relative radiation sensitivity",
            "type": "number",
            "default": data_model.relative_rad_sensitivity,
            "hidden": True,
        }
        fields["budget_use_fraction"] = {
            "title": "Budget fraction to use",
            "type": "number",
            "default": budget_use_fraction,
            "hidden": True,
        }
        fields["maximum_dose_budget"] = {
            "title": "Default value for exposure",
            "type": "number",
            "default": default_exposure,
            "hidden": True,
        }
        # From here on visible fields
        fields["_info"] = {
            # "title": "Data collection plan",
            "type": "string",
            "default": info_text,
            "readOnly":True,
        }
        fields["image_width"] = {
            "title": "Oscillation range",
            "type": "string",
            "default": str(default_image_width),
            "$ref": "#/definitions/image_width",
        }
        fields["exposure"] = {
            "title": "Exposure Time (s)",
            "type": "number",
            "default": default_exposure,
            "minimum": exposure_limits[0],
            "maximum": exposure_limits[1],
        }
        fields["dose_budget"] = {
            "title": "Dose budget (MGy)",
            "type": "number",
            "default": dose_budget,
            "minimum": 0.0,
            "readOnly": True,
        }
        fields["use_dose"] = {
            "title": dose_label,
            "type": "number",
            "default": use_dose_start,
            "minimum": 0.000001,
            "readOnly": use_dose_frozen,
        }
        # NB Transmission is in % in UI, but in 0-1 in workflow
        fields["transmission"] = {
            "title": "Transmission (%)",
            "type": "number",
            "default": transmission,
            "minimum": 0.001,
            "maximum": 100.0,
        }
        fields["resolution"] = {
            "title": "Detector resolution (Å)",
            "type": "number",
            "default": resolution,
            "minimum": reslimits[0],
            "maximum": reslimits[1],
            "readOnly": data_model.characterisation_done,
        }
        fields["experiment_time"] = {
            "title": "Experiment duration (s)",
            "type": "number",
            "default": experiment_time,
            "readOnly": True,
        }
        if data_model.characterisation_done:
            fields["repetition_count"] = {
                "title": "Number of repetitions",
                "type": "spinbox",
                "defaultValue": 1,
                "lowerBound": 1,
                "upperBound": 99,
                "stepsize": 1,
            }

        # if data_model.characterisation_done and data_model.interleave_order:
        if is_interleaved:
            # NB We do not want the wedgeWdth widget for Diffractcal
            fields["wedge_width"] = {
                "title": "Wedge width (°)",
                "type": "number",
                "default": self.settings.get("default_wedge_width", 15),
                "minimum": 0.1,
                "maximum": 7200,
            }
        readonly = True
        energy_limits = HWR.beamline.energy.get_limits()
        for tag, val in beam_energies.items():
            fields[tag] = {
                "title": "%s beam energy (keV)" % tag,
                "type": "number",
                "default": val,
                "minimum": energy_limits[0],
                "maximum": energy_limits[1],
                "readOnly": readonly,
            }
            readonly = False

        fields["snapshot_count"] = {
            "title": "Number of snapshots",
            "type": "string",
            "default": str(data_model.snapshot_count),
            "$ref": "#/definitions/snapshot_count",
        }

        # recentring mode:
        labels = list(RECENTRING_MODES.keys())
        modes = list(RECENTRING_MODES.values())
        default_recentring_mode = self.settings.get("default_recentring_mode", "sweep")
        if default_recentring_mode == "scan" or default_recentring_mode not in modes:
            raise ValueError(
                "invalid default recentring mode '%s' " % default_recentring_mode
            )
        use_modes = ["sweep"]
        if len(orientations) > 1:
            use_modes.append("start")
        # if data_model.interleave_order:
        if is_interleaved:
            use_modes.append("scan")
        if self.load_transcal_parameters() and (
            data_model.characterisation_done
            or wf_parameters.get("strategy_type") == "diffractcal"
        ):
            # Not Characterisation
            use_modes.append("none")
        for indx in range (len(modes) -1, -1, -1):
            if modes[indx] not in use_modes:
                del modes[indx]
                del labels[indx]
        if default_recentring_mode in modes:
            indx = modes.index(default_recentring_mode)
            if indx:
                # Put default at top
                del modes[indx]
                modes.insert(indx, default_recentring_mode)
                default_label = labels.pop(indx)
                labels.insert(indx, default_label)
        else:
            default_recentring_mode = "sweep"
        default_label = labels[modes.index(default_recentring_mode)]
        # if len(modes) > 1:
        fields["recentring_mode"] = {
            # "title": "Recentring mode",
            "type": "string",
            "default": default_label,
            "$ref": "#/definitions/recentring_mode",
        }
        schema["definitions"]["recentring_mode"] = list(
            {
                "type": "string",
                "enum": [label],
                "title": label,
            }
            for label in labels
        )
        schema["definitions"]["snapshot_count"] = list(
            {
                "type": "string",
                "enum": [tag],
                "title": tag,
            }
            for tag in ("0", "1", "2", "4", )
        )
        schema["definitions"]["image_width"] = list(
            {
                "type": "string",
                "enum": [str(tag)],
                "title": str(tag),
            }
            for tag in allowed_widths
        )

        ui_schema = {
            "ui:order": ["_info", "parameters"],
            "ui:widget": "vertical_box",
            "_info": {
                "ui:title": "",
                "ui:widget": "textarea",
                "ui:options": {
                    # 'rows' not used in Qt - gives minimum necessary size.
                    "rows": len(fields["_info"]["default"].splitlines()) + 1
                }
            },
            "parameters": {
                "ui:title": "Parameters",
                "ui:widget": "column_grid",
                "ui:order": ["column1", "column2"],
                "column1": {
                    "ui:order": [
                        "use_dose",
                        "exposure",
                        "image_width",
                        "transmission",
                        "snapshot_count",
                    ],
                    "exposure": {
                        "ui:options": {
                            "update_function": "update_exptime",
                            "decimals": 4,
                        }
                    },
                    "use_dose": {
                        "ui:options": {
                            "decimals": use_dose_decimals,
                            "update_function": "update_dose",
                        }
                    },
                    "image_width": {
                        "ui:options": {
                            "update_function": "update_dose",
                        }
                    },
                    "transmission": {
                        "ui:options": {
                            "decimals": 2,
                            "update_function": "update_transmission",
                        }
                    },
                    "repetition_count": {
                        "ui:options": {
                        }
                    },
                },
                "column2": {
                    "ui:order": [
                        "dose_budget",
                        "experiment_time",
                        "resolution",
                    ],
                    "dose_budget": {
                        "ui:options": {
                            "decimals": 3,
                        }
                    },
                    "resolution": {
                        "ui:options": {
                            "decimals": 3,
                            "update_function": "update_resolution",
                        }
                    },
                    "experiment_time": {
                        "ui:options": {
                            "decimals": 1,
                        }
                    },
                },
            }
        }
        if data_model.characterisation_done and is_interleaved:
            ui_schema["parameters"]["column1"]["ui:order"].append("wedge_width")
        if data_model.characterisation_done:
            ui_schema["parameters"]["column1"]["ui:order"].insert(-1, "repetition_count", )

        ll0 =  ui_schema["parameters"]["column2"]["ui:order"]
        ll0.extend(list(beam_energies))
        ll0.append("recentring_mode")

        self._return_parameters = gevent.event.AsyncResult()
        try:
            responses = dispatcher.send(
                "gphlJsonParametersNeeded",
                self,
                schema,
                ui_schema,
                self._return_parameters,
            )
            if not responses:
                self._return_parameters.set_exception(
                    RuntimeError("Signal 'gphlJsonParametersNeeded' is not connected")
                )

            result = self._return_parameters.get()
            if result is StopIteration:
                return StopIteration
        finally:
            self._return_parameters = None
        # print ('@~@~ query_collection_strategy returned parameters 1')
        # for item in result.items():
        #     print ('--> %s : %s' % item)

        tag = "image_width"
        value = result.get(tag)
        if value:
            image_width = float(value)
        else:
            image_width = self.settings.get("default_image_width", default_image_width)
        result[tag] = image_width
        # exposure OK as is
        tag = "repetition_count"
        result[tag] = int(result.get(tag) or 1)
        tag = "transmission"
        value = result.get(tag)
        if value:
            # Convert from % to fraction
            result[tag] = value / 100.
        tag = "wedgeWidth"
        value = result.get(tag)
        if value:
            result[tag] = int(value / image_width)
        else:
            # If not set is likely not used, but we want a default value anyway
            result[tag] = 150
        # resolution OK as is

        tag = "snapshot_count"
        value = result.get(tag)
        if value:
            result[tag] = int(value)

        if geometric_strategy.isInterleaved:
            result["interleave_order"] = data_model.interleave_order

        energies = list(result.pop(tag) for tag in beam_energies)
        del energies[0]
        result["energies"] = energies

        tag = "recentring_mode"
        result[tag] = (
            RECENTRING_MODES.get(result.get(tag)) or default_recentring_mode
        )

        # data_model.dose_budget = float(params.get("dose_budget", 0))
        # # Register the dose (about to be) consumed
        if std_dose_rate:
            if (
                data_model.characterisation_done
                or wf_parameters.get("strategy_type") == "diffractcal"
            ):
                data_model.acquisition_dose = float(result.get("use_dose", 0))
            else:
                data_model.characterisation_dose = float(result.get("use_dose", 0))
        # print ('@~@~ query_collection_strategy returned parameters 3')
        # for item in result.items():
        #     print ('--> %s : %s' % item)
        #
        return result

    def setup_data_collection(self, payload, correlation_id):
        """Query data collection parameters and return SampleCentred to ASTRA workflow

        :param payload (GphlMessages.GeometricStrategy):
        :param correlation_id (int) Astra workflow correlation ID
        :return (GphlMessages.SampleCentred):
        """
        geometric_strategy = payload

        # Set up
        gphl_workflow_model = self._queue_entry.get_data_model()
        strategy_type = gphl_workflow_model.get_workflow_parameters()[
            "strategy_type"
        ]
        sweeps = geometric_strategy.get_ordered_sweeps()

        # Set strategy_length
        strategy_length = sum(sweep.width for sweep in sweeps)
        gphl_workflow_model.strategy_length = (
            strategy_length * len(gphl_workflow_model.wavelengths)
        )

        # get parameters and initial transmission/use_dose
        if gphl_workflow_model.automation_mode:
            # Get parameters and transmission/use_dose
            if gphl_workflow_model.characterisation_done:
                parameters = gphl_workflow_model.auto_acq_parameters[-1]
            else:
                parameters = gphl_workflow_model.auto_acq_parameters[0]
            if "dose_budget" in parameters:
                raise ValueError(
                    "'dose_budget' parameter no longer supported. "
                    "Use 'use_dose' or 'transmission' instead"
                )
            transmission = parameters.get("transmission")
            use_dose = parameters.get("use_dose")
        else:
            transmission = None
            use_dose = None

        # set gphl_workflow_model.transmission (initial value for interactive mode)
        if transmission is None:
            # If transmission is already set (automation mode), there is nothing to do
            if use_dose is None:
                # Set use_dose from recommended budget
                use_dose = gphl_workflow_model.recommended_dose_budget()
                if gphl_workflow_model.characterisation_done:
                    use_dose -= gphl_workflow_model.characterisation_dose
                elif strategy_type != "diffractcal":
                    # This is characterisation
                    use_dose *= gphl_workflow_model.characterisation_budget_fraction
            transmission = gphl_workflow_model.calculate_transmission(use_dose)
            if transmission > 100:
                if (
                    gphl_workflow_model.characterisation_done
                    or strategy_type == "diffractcal"
                ):
                    # We are not in characterisation.
                    # Try top reset exposure time to get desired dose
                    exposure_time = (
                        gphl_workflow_model.exposure_time * transmission / 100
                    )
                    exposure_limits = (
                        HWR.beamline.detector.get_exposure_time_limits()
                    )
                    if exposure_limits[1]:
                        exposure_time = max (exposure_limits[1], exposure_time)
                    gphl_workflow_model.exposure_time = exposure_time
                transmission = 100
            gphl_workflow_model.transmission = transmission

        # If not in automation mode, get params from user query
        if not gphl_workflow_model.automation_mode:

            # NB - now pre-setting of detector has been removed, this gets
            # the current resolution setting, whatever it is
            initial_resolution = HWR.beamline.resolution.get_value()
            # Put resolution value in workflow model object
            # gphl_workflow_model.set_detector_resolution(initial_resolution)

            # Get modified parameters from UI and confirm acquisition
            # Run before centring, as it also does confirm/abort
            parameters = self.query_collection_strategy(geometric_strategy)
            if parameters is StopIteration:
                return StopIteration
            user_modifiable = geometric_strategy.isUserModifiable
            if user_modifiable:
                # Query user for new rotationSetting and make it,
                logging.getLogger("HWR").warning(
                    "User modification of sweep settings not implemented. Ignored"
                )

            gphl_workflow_model.exposure_time = parameters.get("exposure" or 0.0)
            gphl_workflow_model.image_width = parameters.get("image_width" or 0.0)

            transmission = parameters["transmission"]
            logging.getLogger("GUI").info(
                "GphlWorkflow: setting transmission to %7.3f %%" % (100.0 * transmission)
            )
            HWR.beamline.transmission.set_value(100 * transmission)
            gphl_workflow_model.transmission = transmission

            new_resolution = parameters.pop("resolution")
            if (
                new_resolution != initial_resolution
                and not gphl_workflow_model.characterisation_done
            ):
                logging.getLogger("GUI").info(
                    "GphlWorkflow: setting detector distance for resolution %7.3f A"
                    % new_resolution
                )
                # timeout in seconds: max move is ~2 meters, velocity 4 cm/sec
                HWR.beamline.resolution.set_value(new_resolution, timeout=60)

            snapshot_count = parameters.pop("snapshot_count", None)
            if snapshot_count is not None:
                gphl_workflow_model.snapshot_count = snapshot_count

            gphl_workflow_model.recentring_mode = parameters.pop("recentring_mode")

        # From here on same for manual and automation
        gphl_workflow_model.set_pre_acquisition_params(**parameters)

        # Update dose consumed to include dose (about to be) acquired.
        new_dose = gphl_workflow_model.calculate_dose()
        if (
            gphl_workflow_model.characterisation_done
            or gphl_workflow_model.get_type() == "diffractcal"
        ):
            gphl_workflow_model.acquisition_dose = new_dose
        else:
            gphl_workflow_model.characterisation_dose = new_dose

        format = "--> %s: %s"
        print ("GPHL workflow. Data collection parameters:")
        for item in gphl_workflow_model.parameter_summary().items():
            print (format % item)

        # Enqueue data collection
        if gphl_workflow_model.characterisation_done:
            # Data collection TODO: Use workflow info to distinguish
            new_dcg_name = "GPhL Data Collection"
        elif strategy_type == "diffractcal":
            new_dcg_name = "GPhL DiffractCal"
        else:
            new_dcg_name = "GPhL Characterisation"
        logging.getLogger("HWR").debug("setup_data_collection %s" % new_dcg_name)
        new_dcg_model = queue_model_objects.TaskGroup()
        new_dcg_model.set_enabled(True)
        new_dcg_model.set_name(new_dcg_name)
        new_dcg_model.set_number(
            gphl_workflow_model.get_next_number_for_name(new_dcg_name)
        )
        self._data_collection_group = new_dcg_model
        # self._add_to_queue(gphl_workflow_model, new_dcg_model)

        #
        # Set (re)centring behaviour and goniostatTranslations
        #
        recentring_mode = gphl_workflow_model.recentring_mode
        recen_parameters = self.load_transcal_parameters()
        goniostatTranslations = []

        # Get all sweepSettings, in order
        sweepSettings = []
        sweepSettingIds = set()
        for sweep in sweeps:
            sweepSetting = sweep.goniostatSweepSetting
            sweepSettingId = sweepSetting.id_
            if sweepSettingId not in sweepSettingIds:
                sweepSettingIds.add(sweepSettingId)
                sweepSettings.append(sweepSetting)

        # For recentring mode 'start' do settings in reverse order
        if recentring_mode == "start":
            sweepSettings.reverse()

        # Handle centring of first orientation
        pos_dict = HWR.beamline.diffractometer.get_positions()
        sweepSetting = sweepSettings[0]
        if (
            self.settings.get("recentre_before_start")
            and not gphl_workflow_model.characterisation_done
        ):
            # Sample has never been centred reliably.
            # Centre it at sweepsetting and put it into goniostatTranslations
            settings = dict(sweepSetting.axisSettings)
            qe = self.enqueue_sample_centring(motor_settings=settings)
            translation, current_pos_dict = self.execute_sample_centring(
                qe, sweepSetting
            )
            goniostatTranslations.append(translation)
            gphl_workflow_model.current_rotation_id = sweepSetting.id_
        else:
            # Sample was centred already, possibly during earlier characterisation
            # - use current position for recentring
            current_pos_dict = HWR.beamline.diffractometer.get_positions()
        current_okp = tuple(current_pos_dict[role] for role in self.rotation_axis_roles)
        current_xyz = tuple(
            current_pos_dict[role] for role in self.translation_axis_roles
        )
        if recen_parameters:
            # Currrent position is now centred one way or the other
            # Update recentring parameters
            recen_parameters["ref_xyz"] = current_xyz
            recen_parameters["ref_okp"] = current_okp
            logging.getLogger("HWR").debug(
                "Recentring set-up. Parameters are: %s",
                sorted(recen_parameters.items()),
            )
        if goniostatTranslations:
            # We had recentre_before_start and already have the goniosatTranslation
            # matching the sweepSetting
            pass

        elif gphl_workflow_model.characterisation_done or strategy_type == "diffractcal":
            # Acquisition or diffractcal; crystal is already centred
            settings = dict(sweepSetting.axisSettings)
            okp = tuple(settings.get(x, 0) for x in self.rotation_axis_roles)
            maxdev = max(abs(okp[1] - current_okp[1]), abs(okp[2] - current_okp[2]))

            if recen_parameters:
                # recentre first sweep from okp
                tol = self.settings.get("angular_tolerance", 1.0)
                translation_settings = self.calculate_recentring(
                    okp, **recen_parameters
                )
                logging.getLogger("HWR").debug(
                    "GPHL Recentring. okp, motors, %s, %s"
                    % (okp, sorted(translation_settings.items()))
                )
            else:
                # existing centring - take from current position
                tol = 0.1
                translation_settings = dict(
                    (role, current_pos_dict.get(role))
                    for role in self.translation_axis_roles
                )

            if maxdev <= tol:
                # first orientation matches current, set to current centring
                # Use sweepSetting as is, recentred or very close
                translation = GphlMessages.GoniostatTranslation(
                    rotation=sweepSetting, **translation_settings
                )
                goniostatTranslations.append(translation)
                gphl_workflow_model.current_rotation_id = sweepSetting.id_

            else:

                if recentring_mode == "none":
                    if recen_parameters:
                        translation = GphlMessages.GoniostatTranslation(
                            rotation=sweepSetting, **translation_settings
                        )
                        goniostatTranslations.append(translation)
                    else:
                        raise RuntimeError(
                            "Coding error, mode 'none' requires recen_parameters"
                        )
                else:
                    settings.update(translation_settings)
                    qe = self.enqueue_sample_centring(motor_settings=settings)
                    translation, dummy = self.execute_sample_centring(qe, sweepSetting)
                    goniostatTranslations.append(translation)
                    gphl_workflow_model.current_rotation_id = sweepSetting.id_
                    if recentring_mode == "start":
                        # We want snapshots in this mode,
                        # and the first sweepmis skipped in the loop below
                        okp = tuple(
                            int(settings.get(x, 0)) for x in self.rotation_axis_roles
                        )
                        self.collect_centring_snapshots("%s_%s_%s" % okp)

        elif not self.settings.get("recentre_before_start"):
            # Characterisation, and current position was pre-centred
            # Do characterisation at current position, not the hardcoded one
            rotation_settings = dict(
                (role, current_pos_dict[role]) for role in sweepSetting.axisSettings
            )
            newRotation = GphlMessages.GoniostatRotation(**rotation_settings)
            translation_settings = dict(
                (role, current_pos_dict.get(role))
                for role in self.translation_axis_roles
            )
            translation = GphlMessages.GoniostatTranslation(
                rotation=newRotation,
                requestedRotationId=sweepSetting.id_,
                **translation_settings
            )
            goniostatTranslations.append(translation)
            gphl_workflow_model.current_rotation_id = newRotation.id_

        # calculate or determine centring for remaining sweeps
        if not goniostatTranslations:
            raise RuntimeError(
                "Coding error, first sweepSetting should have been set here"
            )
        for sweepSetting in sweepSettings[1:]:
            settings = sweepSetting.get_motor_settings()
            if recen_parameters:
                # Update settings
                okp = tuple(settings.get(x, 0) for x in self.rotation_axis_roles)
                centring_settings = self.calculate_recentring(okp, **recen_parameters)
                logging.getLogger("HWR").debug(
                    "GPHL Recentring. okp, motors, %s, %s"
                    % (okp, sorted(centring_settings.items()))
                )
                settings.update(centring_settings)

            if recentring_mode == "start":
                # Recentre now, using updated values if available
                qe = self.enqueue_sample_centring(motor_settings=settings)
                translation, dummy = self.execute_sample_centring(qe, sweepSetting)
                goniostatTranslations.append(translation)
                gphl_workflow_model.current_rotation_id = sweepSetting.id_
                okp = tuple(int(settings.get(x, 0)) for x in self.rotation_axis_roles)
                self.collect_centring_snapshots("%s_%s_%s" % okp)
            elif recen_parameters:
                # put recalculated translations back to workflow
                translation = GphlMessages.GoniostatTranslation(
                    rotation=sweepSetting, **centring_settings
                )
                goniostatTranslations.append(translation)
            else:
                # Not supposed to centre, no recentring parameters
                # NB PK says 'just provide the centrings you actually have'
                # raise NotImplementedError(
                #     "For now must have recentring or mode 'start' or single sweep"
                # )
                # We do NOT have any sensible translation settings
                # Take the current settings because we need something.
                # Better to have a calibration, actually
                translation_settings = dict(
                    (role, pos_dict[role]) for role in self.translation_axis_roles
                )
                translation = GphlMessages.GoniostatTranslation(
                    rotation=sweepSetting, **translation_settings
                )
                goniostatTranslations.append(translation)
        #
        gphl_workflow_model.goniostat_translations = goniostatTranslations

        # Do it here so that any centring actions are enqueued dfirst
        self._add_to_queue(gphl_workflow_model, new_dcg_model)

        # Return SampleCentred message
        sampleCentred = GphlMessages.SampleCentred(gphl_workflow_model)
        return sampleCentred

    def load_transcal_parameters(self):
        """Load home_position and cross_sec_of_soc from transcal.nml"""
        fp0 = self.file_paths.get("transcal_file")
        if os.path.isfile(fp0):
            try:
                transcal_data = f90nml.read(fp0)["sdcp_instrument_list"]
            except Exception:
                logging.getLogger("HWR").error(
                    "Error reading transcal.nml file: %s", fp0
                )
            else:
                result = {}
                result["home_position"] = transcal_data.get("trans_home")
                result["cross_sec_of_soc"] = transcal_data.get("trans_cross_sec_of_soc")
                if None in result.values():
                    logging.getLogger("HWR").warning("load_transcal_parameters failed")
                else:
                    return result
        else:
            logging.getLogger("HWR").warning("transcal.nml file not found: %s", fp0)
        # If we get here reading failed
        return {}

    def calculate_recentring(
        self, okp, home_position, cross_sec_of_soc, ref_okp, ref_xyz
    ):
        """Add predicted traslation values using recen
        okp is the omega,gamma,phi tuple of the target position,
        home_position is the translation calibration home position,
        and cross_sec_of_soc is the cross-section of the sphere of confusion
        ref_okp and ref_xyz are the reference omega,gamma,phi and the
        corresponding x,y,z translation position"""

        # Make input file
        software_paths = HWR.beamline.gphl_connection.software_paths
        infile = os.path.join(software_paths["GPHL_WDIR"], "temp_recen.in")
        recen_data = OrderedDict()
        indata = {"recen_list": recen_data}

        fp0 = self.file_paths.get("instrumentation_file")
        instrumentation_data = f90nml.read(fp0)["sdcp_instrument_list"]
        diffractcal_data = instrumentation_data

        fp0 = self.file_paths.get("diffractcal_file")
        try:
            diffractcal_data = f90nml.read(fp0)["sdcp_instrument_list"]
        except BaseException:
            logging.getLogger("HWR").debug(
                "diffractcal file not present - using instrumentation.nml %s", fp0
            )
        ll0 = diffractcal_data["gonio_axis_dirs"]
        recen_data["omega_axis"] = ll0[:3]
        recen_data["kappa_axis"] = ll0[3:6]
        recen_data["phi_axis"] = ll0[6:]
        ll0 = instrumentation_data["gonio_centring_axis_dirs"]
        recen_data["trans_1_axis"] = ll0[:3]
        recen_data["trans_2_axis"] = ll0[3:6]
        recen_data["trans_3_axis"] = ll0[6:]
        recen_data["cross_sec_of_soc"] = cross_sec_of_soc
        recen_data["home"] = home_position
        #
        f90nml.write(indata, infile, force=True)

        # Get program locations
        recen_executable = HWR.beamline.gphl_connection.get_executable("recen")
        # Get environmental variables
        envs = {}
        GPHL_XDS_PATH =  HWR.beamline.gphl_connection.software_paths.get("GPHL_XDS_PATH")
        if GPHL_XDS_PATH:
            envs["GPHL_XDS_PATH"] = GPHL_XDS_PATH
        GPHL_CCP4_PATH =  HWR.beamline.gphl_connection.software_paths.get("GPHL_CCP4_PATH")
        if GPHL_CCP4_PATH:
            envs["GPHL_CCP4_PATH"] = GPHL_CCP4_PATH
        # Run recen
        command_list = [
            recen_executable,
            "--input",
            infile,
            "--init-xyz",
            "%s,%s,%s" % ref_xyz,
            "--init-okp",
            "%s,%s,%s" % ref_okp,
            "--okp",
            "%s,%s,%s" % okp,
        ]
        # NB the universal_newlines has the NECESSARY side effect of converting
        # output from bytes to string (with default encoding),
        # avoiding an explicit decoding step.
        result = {}
        logging.getLogger("HWR").debug(
            "Running Recen command: %s", " ".join(command_list)
        )
        try:
            output = subprocess.check_output(
                command_list,
                env=envs,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
        except subprocess.CalledProcessError as err:
            logging.getLogger("HWR").error(
                "Recen failed with returncode %s. Output was:\n%s",
                err.returncode,
                err.output,
            )
            return result

        terminated_ok = False
        for line in reversed(output.splitlines()):
            ss0 = line.strip()
            if terminated_ok:
                if "X,Y,Z" in ss0:
                    ll0 = ss0.split()[-3:]
                    for ii, tag in enumerate(self.translation_axis_roles):
                        result[tag] = float(ll0[ii])
                    break

            elif ss0 == "NORMAL termination":
                terminated_ok = True
        else:
            logging.getLogger("HWR").error(
                "Recen failed with normal termination=%s. Output was:\n" % terminated_ok
                + output
            )
        #
        return result

    def collect_data(self, payload, correlation_id):
        collection_proposal = payload

        angular_tolerance = float(self.settings.get("angular_tolerance", 1.0))
        queue_manager = self._queue_entry.get_queue_controller()

        gphl_workflow_model = self._queue_entry.get_data_model()

        if gphl_workflow_model.init_spot_dir:
            # Characterisation, where collection and XDS have alredy been run
            return GphlMessages.CollectionDone(
                status=0,
                proposalId=collection_proposal.id_,
                # Only if you want to override prior information rootdir,
                # imageRoot=gphl_workflow_model.characterisation_directory
            )

        master_path_template = gphl_workflow_model.path_template
        relative_image_dir = collection_proposal.relativeImageDir

        sample = gphl_workflow_model.get_sample_node()
        # There will be exactly one for the kinds of collection we are doing
        crystal = sample.crystals[0]
        snapshot_count = gphl_workflow_model.snapshot_count
        # wf_parameters = gphl_workflow_model.get_workflow_parameters()
        # if (
        #     gphl_workflow_model.characterisation_done
        #     or wf_parameters.get("strategy_type") == "diffractcal"
        # ):
        #     snapshot_count = gphl_workflow_model.get_snapshot_count()
        # else:
        #     # Do not make snapshots during chareacterisation
        #     snapshot_count = 0
        recentring_mode = gphl_workflow_model.recentring_mode
        data_collections = []
        scans = collection_proposal.scans

        geometric_strategy = collection_proposal.strategy
        repeat_count = geometric_strategy.sweepRepeat
        sweep_offset = geometric_strategy.sweepOffset
        scan_count = len(scans)

        if repeat_count and sweep_offset and self.settings.get("use_multitrigger"):
            # commpress unrolled multi-trigger sweep
            # NBNB as of 202103 this is only allowed for a single sweep
            #
            # For now this is required
            if repeat_count != scan_count:
                raise ValueError(
                    " scan count %s does not match repreat count %s"
                    % (scan_count, repeat_count)
                )
            # treat only the first scan
            scans = scans[:1]

        sweeps = set()
        last_orientation = ()
        maxdev = -1
        snapshotted_rotation_ids = set()
        for scan in scans:
            sweep = scan.sweep
            goniostatRotation = sweep.goniostatSweepSetting
            rotation_id = goniostatRotation.id_
            initial_settings = sweep.get_initial_settings()
            orientation = tuple(
                initial_settings.get(tag) for tag in ("kappa", "kappa_phi")
            )
            acq = queue_model_objects.Acquisition()

            # Get defaults, even though we override most of them
            acq_parameters = HWR.beamline.get_default_acquisition_parameters()
            acq.acquisition_parameters = acq_parameters

            acq_parameters.first_image = scan.imageStartNum
            acq_parameters.num_images = scan.width.numImages
            acq_parameters.osc_start = scan.start
            acq_parameters.osc_range = scan.width.imageWidth
            logging.getLogger("HWR").info(
                "Scan: %s images of %s deg. starting at %s (%s deg)",
                acq_parameters.num_images,
                acq_parameters.osc_range,
                acq_parameters.first_image,
                acq_parameters.osc_start,
            )
            acq_parameters.exp_time = scan.exposure.time
            acq_parameters.num_passes = 1

            ##
            wavelength = sweep.beamSetting.wavelength
            acq_parameters.energy = HWR.beamline.energy.calculate_energy(wavelength)
            detdistance = sweep.detectorSetting.axisSettings["Distance"]
            # not needed when detdistance is set :
            # acq_parameters.resolution = resolution
            acq_parameters.detector_distance = detdistance
            # transmission is not passed from the workflow (yet)
            # it defaults to current value (?), so no need to set it
            # acq_parameters.transmission = transmission*100.0

            # acq_parameters.shutterless = self._has_shutterless()
            # acq_parameters.detector_mode = self._get_roi_modes()
            acq_parameters.inverse_beam = False
            # acq_parameters.take_dark_current = True
            # acq_parameters.skip_existing_images = False

            # Edna also sets screening_id
            # Edna also sets osc_end

            # Path_template
            path_template = HWR.beamline.get_default_path_template()
            # Naughty, but we want a clone, right?
            # NBNB this ONLY works because all the attributes are immutable values
            path_template.__dict__.update(master_path_template.__dict__)
            if relative_image_dir:
                path_template.directory = os.path.join(
                    HWR.beamline.session.get_base_image_directory(), relative_image_dir
                )
                path_template.process_directory = os.path.join(
                    HWR.beamline.session.get_base_process_directory(), relative_image_dir
                )
            acq.path_template = path_template
            filename_params = scan.filenameParams
            subdir = filename_params.get("subdir")
            if subdir:
                path_template.directory = os.path.join(path_template.directory, subdir)
                path_template.process_directory = os.path.join(
                    path_template.process_directory, subdir
                )
            ss0 = filename_params.get("run")
            path_template.run_number = int(ss0) if ss0 else 1
            path_template.base_prefix = filename_params.get("prefix", "")
            path_template.start_num = acq_parameters.first_image
            path_template.num_files = acq_parameters.num_images

            if last_orientation:
                maxdev = max(
                    abs(orientation[ind] - last_orientation[ind])
                    for ind in range(2)
                )
            if sweeps and (
                recentring_mode == "scan"
                or (
                    recentring_mode == "sweep"
                    and not (
                        rotation_id == gphl_workflow_model.current_rotation_id
                        or (0 <= maxdev < angular_tolerance)
                    )
                )
            ):
                # Put centring on queue and collect using the resulting position
                # NB this means that the actual translational axis positions
                # will NOT be known to the workflow
                #
                # Done if 1) we centre for each acan
                # 2) we centre for each sweep unless the rotation ID is unchanged
                #    or the kappa and phi values are unchanged anyway
                self.enqueue_sample_centring(
                    motor_settings=initial_settings, in_queue=True
                )
            else:
                # Collect using precalculated centring position
                initial_settings[goniostatRotation.scanAxis] = scan.start
                acq_parameters.centred_position = queue_model_objects.CentredPosition(
                    initial_settings
                )

            if (
                rotation_id in snapshotted_rotation_ids
                and rotation_id == gphl_workflow_model.current_rotation_id
            ):
                acq_parameters.take_snapshots = 0
            else:
                # Only snapshots at the start or when orientation changes
                # NB the current_rotation_id can be set before acquisition commences
                # as it controls centring
                snapshotted_rotation_ids.add(rotation_id)
                acq_parameters.take_snapshots = snapshot_count
            gphl_workflow_model.current_rotation_id = rotation_id

            sweeps.add(sweep)
            last_orientation = orientation

            if repeat_count and sweep_offset and self.settings.get("use_multitrigger"):
                # Multitrigger sweep - add in parameters.
                # NB if we are here ther can be only one scan
                acq_parameters.num_triggers = scan_count
                acq_parameters.num_images_per_trigger =  acq_parameters.num_images
                acq_parameters.num_images *= scan_count
                # NB this assumes sweepOffset is the offset between starting points
                acq_parameters.overlap = (
                    acq_parameters.num_images_per_trigger * acq_parameters.osc_range
                    - sweep_offset
                )
            data_collection = queue_model_objects.DataCollection([acq], crystal)
            data_collections.append(data_collection)

            data_collection.set_enabled(True)
            data_collection.set_name(path_template.get_prefix())
            data_collection.set_number(path_template.run_number)
            self._add_to_queue(self._data_collection_group, data_collection)
            if scan is not scans[-1]:
                dc_entry = queue_manager.get_entry_with_model(data_collection)
                dc_entry.in_queue = True

        # debug
        format = "--> %s: %s"
        print ("GPHL workflow. Collect with parameters:")
        for item in gphl_workflow_model.parameter_summary().items():
            print (format % item)
        print( format % ("sweep_count", len(sweeps)))

        data_collection_entry = queue_manager.get_entry_with_model(
            self._data_collection_group
        )

        # dispatcher.send("gphlStartAcquisition", self, gphl_workflow_model)
        try:
            queue_manager.execute_entry(data_collection_entry)
        except:
            HWR.beamline.queue_manager.emit("queue_execution_failed", (None,))
        # finally:
        #     dispatcher.send("gphlDoneAcquisition", self, gphl_workflow_model)
        self._data_collection_group = None

        if data_collection_entry.status == QUEUE_ENTRY_STATUS.FAILED:
            # TODO NBNB check if these status codes are corerct
            status = 1
        else:
            status = 0

        return GphlMessages.CollectionDone(
            status=status,
            proposalId=collection_proposal.id_,
            procWithLatticeParams=gphl_workflow_model.use_cell_for_processing,
        )

    def select_lattice(self, payload, correlation_id):

        choose_lattice = payload

        data_model = self._queue_entry.get_data_model()
        data_model.characterisation_done = True

        if data_model.automation_mode:
            header, soldict, select_row = self.parse_indexing_solution(choose_lattice)

            indexingSolution = soldict.values()[select_row]

            if not data_model.aimed_resolution:
                raise ValueError(
                    "aimed_resolution must be set in automation mode"
                )
            # Resets detector_setting to match aimed_resolution
            data_model.detector_setting = None
            # NB resets detector_setting
            params = data_model.auto_acq_parameters[-1]
            if "resolution" not in params:
                params["resolution"] = (
                    data_model.aimed_resolution
                    or HWR.beamline.get_default_acquisition_parameters().resolution
                )
        else:
            # SIGNAL TO GET Pre-strategy parameters here
            # NB set defaults from data_model
            # NB consider whether to override on None

            # NBNB DISPLAY_ENERGY_DECIMALS

            params = self.query_pre_strategy_params(choose_lattice)
            indexingSolution = params["indexing_solution"]
        data_model.set_pre_strategy_params(**params)
        # @~@~ DEBUG:
        format = "--> %s: %s"
        print ("GPHL workflow. Lattice selected with parameters:")
        for item in data_model.parameter_summary().items():
            print (format % item)
        distance = data_model.detector_setting.axisSettings["Distance"]
        HWR.beamline.detector.distance.set_value(distance, timeout=30)
        return GphlMessages.SelectedLattice(
            data_model, indexingSolution=indexingSolution
        )


    def parse_indexing_solution(self, choose_lattice):
        """

        Args:
            choose_lattice GphlMessages.ChooseLattice:

        Returns: tuple

        """

        # Solution table. for format IDXREF will look like
        """
*********** DETERMINATION OF LATTICE CHARACTER AND BRAVAIS LATTICE ***********

 The CHARACTER OF A LATTICE is defined by the metrical parameters of its
 reduced cell as described in the INTERNATIONAL TABLES FOR CRYSTALLOGRAPHY
 Volume A, p. 746 (KLUWER ACADEMIC PUBLISHERS, DORDRECHT/BOSTON/LONDON, 1989).
 Note that more than one lattice character may have the same BRAVAIS LATTICE.

 A lattice character is marked "*" to indicate a lattice consistent with the
 observed locations of the diffraction spots. These marked lattices must have
 low values for the QUALITY OF FIT and their implicated UNIT CELL CONSTANTS
 should not violate the ideal values by more than
 MAXIMUM_ALLOWED_CELL_AXIS_RELATIVE_ERROR=  0.03
 MAXIMUM_ALLOWED_CELL_ANGLE_ERROR=           1.5 (Degrees)

  LATTICE-  BRAVAIS-   QUALITY  UNIT CELL CONSTANTS (ANGSTROEM & DEGREES)
 CHARACTER  LATTICE     OF FIT      a      b      c   alpha  beta gamma

 *  44        aP          0.0      56.3   56.3  102.3  90.0  90.0  90.0
 *  31        aP          0.0      56.3   56.3  102.3  90.0  90.0  90.0
 *  33        mP          0.0      56.3   56.3  102.3  90.0  90.0  90.0
 *  35        mP          0.0      56.3   56.3  102.3  90.0  90.0  90.0
 *  34        mP          0.0      56.3  102.3   56.3  90.0  90.0  90.0
 *  32        oP          0.0      56.3   56.3  102.3  90.0  90.0  90.0
 *  14        mC          0.1      79.6   79.6  102.3  90.0  90.0  90.0
 *  10        mC          0.1      79.6   79.6  102.3  90.0  90.0  90.0
 *  13        oC          0.1      79.6   79.6  102.3  90.0  90.0  90.0
 *  11        tP          0.1      56.3   56.3  102.3  90.0  90.0  90.0
    37        mC        250.0     212.2   56.3   56.3  90.0  90.0  74.6
    36        oC        250.0      56.3  212.2   56.3  90.0  90.0 105.4
    28        mC        250.0      56.3  212.2   56.3  90.0  90.0  74.6
    29        mC        250.0      56.3  125.8  102.3  90.0  90.0  63.4
    41        mC        250.0     212.3   56.3   56.3  90.0  90.0  74.6
    40        oC        250.0      56.3  212.2   56.3  90.0  90.0 105.4
    39        mC        250.0     125.8   56.3  102.3  90.0  90.0  63.4
    30        mC        250.0      56.3  212.2   56.3  90.0  90.0  74.6
    38        oC        250.0      56.3  125.8  102.3  90.0  90.0 116.6
    12        hP        250.1      56.3   56.3  102.3  90.0  90.0  90.0
    27        mC        500.0     125.8   56.3  116.8  90.0 115.5  63.4
    42        oI        500.0      56.3   56.3  219.6 104.8 104.8  90.0
    15        tI        500.0      56.3   56.3  219.6  75.2  75.2  90.0
    26        oF        625.0      56.3  125.8  212.2  83.2 105.4 116.6
     9        hR        750.0      56.3   79.6  317.1  90.0 100.2 135.0
     1        cF        999.0     129.6  129.6  129.6 128.6  75.7 128.6
     2        hR        999.0      79.6  116.8  129.6 118.9  90.0 109.9
     3        cP        999.0      56.3   56.3  102.3  90.0  90.0  90.0
     5        cI        999.0     116.8   79.6  116.8  70.1  39.8  70.1
     4        hR        999.0      79.6  116.8  129.6 118.9  90.0 109.9
     6        tI        999.0     116.8  116.8   79.6  70.1  70.1  39.8
     7        tI        999.0     116.8   79.6  116.8  70.1  39.8  70.1
     8        oI        999.0      79.6  116.8  116.8  39.8  70.1  70.1
    16        oF        999.0      79.6   79.6  219.6  90.0 111.2  90.0
    17        mC        999.0      79.6   79.6  116.8  70.1 109.9  90.0
    18        tI        999.0     116.8  129.6   56.3  64.3  90.0 118.9
    19        oI        999.0      56.3  116.8  129.6  61.1  64.3  90.0
    20        mC        999.0     116.8  116.8   56.3  90.0  90.0 122.4
    21        tP        999.0      56.3  102.3   56.3  90.0  90.0  90.0
    22        hP        999.0      56.3  102.3   56.3  90.0  90.0  90.0
    23        oC        999.0     116.8  116.8   56.3  90.0  90.0  57.6
    24        hR        999.0     162.2  116.8   56.3  90.0  69.7  77.4
    25        mC        999.0     116.8  116.8   56.3  90.0  90.0  57.6
    43        mI        999.0      79.6  219.6   56.3 104.8 135.0  68.8

 For protein crystals the possible space group numbers corresponding  to"""

        solutions = choose_lattice.indexingSolutions
        indexing_format = choose_lattice.indexingFormat
        solutions_dict = OrderedDict()

        if indexing_format == "IDXREF":
            header = """  LATTICE-  BRAVAIS-   QUALITY  UNIT CELL CONSTANTS (ANGSTROEM & DEGREES)
 CHARACTER  LATTICE     OF FIT      a      b      c   alpha  beta gamma"""

            line_format = " %s  %2i        %s %12.1f    %6.1f %6.1f %6.1f %5.1f %5.1f %5.1f"
            consistent_solutions = []
            for solution in solutions:
                if solution.isConsistent:
                    char1 = "*"
                    consistent_solutions.append(solution)
                else:
                    char1 = " "
                tpl = (
                    char1,
                    solution.latticeCharacter,
                    solution.bravaisLattice,
                    solution.qualityOfFit,
                )
                solutions_dict[
                    line_format % (
                            tpl + solution.cell.lengths + solution.cell.angles)
                    ] = solution

            lattices = choose_lattice.lattices or ()
            select_row = None
            if lattices:
                # Select best solution matching lattices
                for ii, solution in enumerate(consistent_solutions):
                    if solution.bravaisLattice in lattices:
                        select_row = ii
                        break
            if select_row is None:
                crystal_family_char = choose_lattice.crystalFamilyChar
                if lattices and not crystal_family_char:
                    # NBNB modify? If no lattice match and no crystal family
                    # filter on having same crystal family
                    aset = set(lattice[0] for lattice in lattices)
                    if len(aset) == 1:
                        crystal_family_char = aset.pop()
                if crystal_family_char:
                    # Select best solution based on crystal family
                    for ii, solution in enumerate(consistent_solutions):
                        if solution.bravaisLattice.startswith(crystal_family_char):
                            select_row = ii
                            break

            if select_row is None:
                # No match found, select on solutions only
                lattice = consistent_solutions[-1].bravaisLattice
                for ii, solution in enumerate(consistent_solutions):
                    if solution.bravaisLattice == lattice:
                        select_row = ii
                        break

        else:
            raise RuntimeError(
                "Indexing format %s not supported" % indexing_format
            )
        #
        return header, solutions_dict, select_row
    def process_centring_request(self, payload, correlation_id):
        # Used for transcal only - anything else is data collection related
        request_centring = payload

        logging.getLogger("user_level_log").info(
            "Start centring no. %s of %s",
            request_centring.currentSettingNo,
            request_centring.totalRotations,
        )

        # Rotate sample to RotationSetting
        goniostatRotation = request_centring.goniostatRotation
        goniostatTranslation = goniostatRotation.translation
        #

        if self._data_collection_group is None:
            gphl_workflow_model = self._queue_entry.get_data_model()
            new_dcg_name = "GPhL Translational calibration"
            new_dcg_model = queue_model_objects.TaskGroup()
            new_dcg_model.set_enabled(True)
            new_dcg_model.set_name(new_dcg_name)
            new_dcg_model.set_number(
                gphl_workflow_model.get_next_number_for_name(new_dcg_name)
            )
            self._data_collection_group = new_dcg_model
            self._add_to_queue(gphl_workflow_model, new_dcg_model)

        if request_centring.currentSettingNo < 2:
            # Start without fine zoom setting
            self._use_fine_zoom = False
        elif not self._use_fine_zoom and goniostatRotation.translation is not None:
            # We are moving to having recentered positions -
            # Set or prompt for fine zoom
            self._use_fine_zoom = True
            zoom_motor = HWR.beamline.sample_view.zoom
            if zoom_motor:
                # Zoom to the last predefined position
                # - that should be the largest magnification
                ll0 = zoom_motor.get_predefined_positions_list()
                if ll0:
                    logging.getLogger("user_level_log").info(
                        "Sample re-centering now active - Zooming in."
                    )
                    zoom_motor.moveToPosition(ll0[-1])
                else:
                    logging.getLogger("HWR").warning(
                        "No predefined positions for zoom motor."
                    )
            else:
                # Ask user to zoom
                info_text = """Automatic sample re-centering is now active
    Switch to maximum zoom before continuing"""
                field_list = [
                    {
                        "variableName": "_info",
                        "uiLabel": "Data collection plan",
                        "type": "textarea",
                        "defaultValue": info_text,
                    }
                ]
                self._return_parameters = gevent.event.AsyncResult()
                responses = dispatcher.send(
                    "gphlParametersNeeded",
                    self,
                    field_list,
                    self._return_parameters,
                    None,
                )
                if not responses:
                    self._return_parameters.set_exception(
                        RuntimeError("Signal 'gphlParametersNeeded' is not connected")
                    )

                # We do not need the result, just to end the waiting
                response = self._return_parameters.get()
                self._return_parameters = None
                if response is StopIteration:
                    return StopIteration

        settings = goniostatRotation.axisSettings.copy()
        if goniostatTranslation is not None:
            settings.update(goniostatTranslation.axisSettings)
        centring_queue_entry = self.enqueue_sample_centring(motor_settings=settings)
        goniostatTranslation, dummy = self.execute_sample_centring(
            centring_queue_entry, goniostatRotation
        )

        if request_centring.currentSettingNo >= request_centring.totalRotations:
            returnStatus = "DONE"
        else:
            returnStatus = "NEXT"
        #
        return GphlMessages.CentringDone(
            returnStatus,
            timestamp=time.time(),
            goniostatTranslation=goniostatTranslation,
        )

    def enqueue_sample_centring(self, motor_settings, in_queue=False):

        # NBNB Should be refactored later and combined with execute_sample_centring
        # Now in_queue==False implies immediate execution

        queue_manager = self._queue_entry.get_queue_controller()
        data_model = self._queue_entry.get_data_model()
        if in_queue:
            parent = self._data_collection_group
        else:
            parent = data_model
        task_label = "Centring (kappa=%0.1f,phi=%0.1f)" % (
            motor_settings.get("kappa"),
            motor_settings.get("kappa_phi"),
        )
        if data_model.automation_mode:
            # Either TEST or MASSIF1
            #m NB Negotiate different location with Olof Svensson
            centring_model = queue_model_objects.addXrayCentring(
                parent,
                name=task_label,
                motor_positions=motor_settings,
                grid_size=None
            )
        else:
            centring_model = queue_model_objects.SampleCentring(
                name=task_label, motor_positions=motor_settings
            )
            self._add_to_queue(parent, centring_model)
        centring_entry = queue_manager.get_entry_with_model(centring_model)
        centring_entry.in_queue = in_queue

        return centring_entry

    def collect_centring_snapshots(self, file_name_prefix="snapshot"):
        """

        :param file_name_prefix: str
        :return:
        """

        gphl_workflow_model = self._queue_entry.get_data_model()
        number_of_snapshots = gphl_workflow_model.snapshot_count
        if number_of_snapshots:
            filename_template = "%s_%s_%s.jpeg"
            snapshot_directory = os.path.join(
                gphl_workflow_model.path_template.get_archive_directory(),
                "centring_snapshots",
            )

            logging.getLogger("user_level_log").info(
                "Post-centring: Taking %d sample snapshot(s)", number_of_snapshots
            )
            collect_hwobj = HWR.beamline.collect
            timestamp = datetime.datetime.now().isoformat().split(".")[0]
            summed_angle = 0.0
            for snapshot_index in range(number_of_snapshots):
                if snapshot_index:
                    HWR.beamline.diffractometer.move_omega_relative(90)
                    summed_angle += 90
                snapshot_filename = filename_template % (
                    file_name_prefix,
                    timestamp,
                    snapshot_index + 1,
                )
                snapshot_filename = os.path.join(snapshot_directory, snapshot_filename)
                logging.getLogger("HWR").debug(
                    "Centring snapshot stored at %s", snapshot_filename
                )
                collect_hwobj._take_crystal_snapshot(snapshot_filename)
            if summed_angle:
                HWR.beamline.diffractometer.move_omega_relative(-summed_angle)

    def execute_sample_centring(
        self, centring_entry, goniostatRotation, requestedRotationId=None
    ):

        queue_manager = self._queue_entry.get_queue_controller()
        try:
            queue_manager.execute_entry(centring_entry)
        except:
            HWR.beamline.queue_manager.emit("queue_execution_failed", (None,))

        centring_result = centring_entry.get_data_model().get_centring_result()
        if centring_result:
            positionsDict = centring_result.as_dict()
            dd0 = dict((x, positionsDict[x]) for x in self.translation_axis_roles)
            return (
                GphlMessages.GoniostatTranslation(
                    rotation=goniostatRotation,
                    requestedRotationId=requestedRotationId,
                    **dd0
                ),
                positionsDict,
            )
        else:
            raise RuntimeError("Centring gave no result")

    def prepare_for_centring(self, payload, correlation_id):

        # TODO Add pop-up confirmation box ('Ready for centring?')

        return GphlMessages.ReadyForCentring()

    def obtain_prior_information(self, payload, correlation_id):

        workflow_model = self._queue_entry.get_data_model()

        # NBNB TODO check this is also OK in MXCuBE3
        image_root = HWR.beamline.session.get_base_image_directory()

        if not os.path.isdir(image_root):
            # This directory must exist by the time the WF software checks for it
            try:
                os.makedirs(image_root)
            except Exception:
                # No need to raise error - program will fail downstream
                logging.getLogger("HWR").error(
                    "Could not create image root directory: %s", image_root
                )

        priorInformation = GphlMessages.PriorInformation(workflow_model, image_root)
        #
        return priorInformation

    # Utility functions

    def resolution2dose_budget(
        self,
        resolution,
        decay_limit=None,
        maximum_dose_budget=None,
        relative_rad_sensitivity=1.0):
        """

        Args:
            resolution (float): resolution in A
            decay_limit (float): min. intensity at resolution edge at experiment end (%)
            maximum_dose_budget (float): maximum allowed dose budget
            relative_rad_sensitivity (float) : relative radiation sensitivity of crystal

        Returns (float): Dose budget (MGy)

        Get resolution-dependent dose budget that gives intensity decay_limit%
        at the end of acquisition for reflections at resolution
        assuming an increase in B factor of 1A^2/MGy

        """
        max_budget = maximum_dose_budget or self.settings.get("maximum_dose_budget", 20)
        decay_limit = decay_limit or self.settings.get("decay_limit", 25)
        result = 2 * resolution * resolution * math.log(100.0 / decay_limit)
        #
        return min(result, max_budget) / relative_rad_sensitivity

    @staticmethod
    def calculate_dose(duration, energy, flux_density):
        """ Calculate dose accumulated by sample

        :param duration (s): Duration of radiation
        :param energy (keV): Energy of ratiation
        :param flux_density: Flux in photons per second per mm^2
        :return: Accumulted dose in MGy
        """

        return (
            duration
            * flux_density
            * HWR.beamline.flux.get_dose_rate_per_photon_per_mmsq(energy)
            * 1.0e-6  # convert to MGy
        )

    def get_emulation_samples(self):
        """ Get list of lims_sample information dictionaries for mock/emulation

        Returns: LIST[DICT]

        """
        crystal_file_name = "crystal.nml"
        result = []
        sample_dir = HWR.beamline.gphl_connection.software_paths.get(
            "gphl_test_samples"
        )
        serial = 0
        if sample_dir and os.path.isdir(sample_dir):
            for path, dirnames, filenames in sorted(os.walk(sample_dir)):
                if crystal_file_name in filenames:
                    data = {}
                    sample_name = os.path.basename(path)
                    indata = f90nml.read(os.path.join(path, crystal_file_name))[
                        "simcal_crystal_list"
                    ]
                    space_group = indata.get("sg_name")
                    cell_lengths = indata.get("cell_dim")
                    cell_angles = indata.get("cell_ang_deg")
                    resolution = indata.get("res_limit_def")

                    location = (serial // 10 + 1, serial % 10 + 1)
                    serial += 1
                    data["containerSampleChangerLocation"] = str(location[0])
                    data["sampleLocation"] = str(location[1])

                    data["sampleName"] = sample_name
                    if cell_lengths:
                        for ii, tag in enumerate(("cellA", "cellB", "cellC")):
                            data[tag] = cell_lengths[ii]
                    if cell_angles:
                        for ii, tag in enumerate(
                            ("cellAlpha", "cellBeta", "cellGamma")
                        ):
                            data[tag] = cell_angles[ii]
                    if space_group:
                        data["crystalSpaceGroup"] = space_group

                    data["experimentType"] = "Default"
                    # data["proteinAcronym"] = self.TEST_SAMPLE_PREFIX
                    data["smiles"] = None
                    data["sampleId"] = 100000 + serial

                    # ISPyB docs:
                    # experimentKind: enum('Default','MAD','SAD','Fixed','OSC',
                    # 'Ligand binding','Refinement', 'MAD - Inverse Beam','SAD - Inverse Beam',
                    # 'MXPressE','MXPressF','MXPressO','MXPressP','MXPressP_SAD','MXPressI','MXPressE_SAD','MXScore','MXPressM',)
                    #
                    # Use "Mad, "SAD", "OSC"
                    dfp = data["diffractionPlan"] = {
                        # "diffractionPlanId": 457980,
                        "experimentKind": "Default",
                        "numberOfPositions": 0,
                        "observedResolution": 0.0,
                        "preferredBeamDiameter": 0.0,
                        "radiationSensitivity": 1.0,
                        "requiredCompleteness": 0.0,
                        "requiredMultiplicity": 0.0,
                        # "requiredResolution": 0.0,
                    }
                    dfp["aimedResolution"] = resolution
                    dfp["diffractionPlanId"] = 5000000 + serial

                    dd0 = EMULATION_DATA.get(sample_name, {})
                    for tag, val in dd0.items():
                        if tag in data:
                            data[tag] = val
                        elif tag in dfp:
                            dfp[tag] = val
                    #
                    result.append(data)
        #
        return result

    def get_emulation_crystal_data(self, sample_name=None):
        """If sample is a test data set for emulation, get crystal data

        Returns:
            Optional[dict]
        """
        if sample_name is None:
            sample_name = (
                self._queue_entry.get_data_model().get_sample_node().get_name()
            )
        crystal_data = None
        hklfile = None
        if sample_name:
            sample_dir = HWR.beamline.gphl_connection.software_paths.get(
                "gphl_test_samples"
            )
            if not sample_dir:
                raise ValueError("Test sample requires gphl_test_samples dir specified")
            sample_dir = os.path.join(sample_dir, sample_name)
            if not os.path.isdir(sample_dir):
                raise RuntimeError("No emulation data found for ", sample_name)
            crystal_file = os.path.join(sample_dir, "crystal.nml")
            if not os.path.isfile(crystal_file):
                raise ValueError(
                    "Emulator crystal data file %s does not exist" % crystal_file
                )
            # in spite of the simcal_crystal_list name this returns an OrderdDict
            crystal_data = f90nml.read(crystal_file)["simcal_crystal_list"]
            if isinstance(crystal_data, list):
                crystal_data = crystal_data[0]
            hklfile = os.path.join(sample_dir, "sample.hkli")
            if not os.path.isfile(hklfile):
                raise ValueError("Emulator hkli file %s does not exist" % hklfile)
        #
        return crystal_data, hklfile

    #
    # Functions for new version of UI handling
    #

    @staticmethod
    def update_exptime(values_map:ui_communication.AbstractValuesMap):
        """When image_width or exposure_time change,
         update rotation_rate, experiment_time and either use_dose or transmission
        In parameter popup"""
        print ('@~@~ executing update_exposure')
        parameters = values_map.get_values_map()
        exposure_time = float(parameters.get("exposure", 0))
        image_width = float(parameters.get("image_width", 0))
        transmission = float(parameters.get("transmission", 0))
        repetition_count = int(parameters.get("repetition_count") or 1)
        total_strategy_length = float(parameters.get("total_strategy_length", 0))
        std_dose_rate = float(parameters.get("std_dose_rate", 0))

        if image_width and exposure_time:
            rotation_rate = image_width / exposure_time
            dd0 = {"rotation_rate": rotation_rate,}
            experiment_time = total_strategy_length / rotation_rate
            if std_dose_rate and transmission:
                # NB - dose is calculated for *one* repetition
                dd0["use_dose"] = (
                    std_dose_rate * experiment_time * transmission / 100.
                )
            dd0["experiment_time"] =  experiment_time * repetition_count
            values_map.set_values(**dd0)

    @staticmethod
    def update_transmission(values_map:ui_communication.AbstractValuesMap):
        """When transmission changes, update use_dose
        In parameter popup"""
        print ('@~@~ executing update_transmission')
        parameters = values_map.get_values_map()
        exposure_time = float(parameters.get("exposure", 0))
        image_width = float(parameters.get("image_width", 0))
        transmission = float(parameters.get("transmission", 0))
        total_strategy_length = float(parameters.get("total_strategy_length", 0))
        std_dose_rate = float(parameters.get("std_dose_rate", 0))
        if image_width and exposure_time and std_dose_rate and transmission:
            # If we get here, Adjust dose
            # NB dose is calculated for *one* repetition
            experiment_time = (
                exposure_time
                * total_strategy_length
                / image_width
            )
            use_dose = (std_dose_rate * experiment_time * transmission / 100)
            values_map.set_values(use_dose=use_dose, exposure=exposure_time)

    @staticmethod
    def update_dose(values_map:ui_communication.AbstractValuesMap):
        """When use_dose changes, update transmission and/or exposure_time
        In parameter popup"""
        print ('@~@~ executing update_dose')
        exposure_limits = HWR.beamline.detector.get_exposure_time_limits()
        parameters = values_map.get_values_map()
        exposure_time = float(parameters.get("exposure", 0))
        image_width = float(parameters.get("image_width", 0))
        use_dose = float(parameters.get("use_dose", 0))
        std_dose_rate = float(parameters.get("std_dose_rate", 0))
        total_strategy_length = float(parameters.get("total_strategy_length", 0))

        if image_width and exposure_time and std_dose_rate and use_dose:
            experiment_time = exposure_time * total_strategy_length / image_width
            transmission = 100 * use_dose / (std_dose_rate * experiment_time)

            if transmission > 100.:
                # Transmission too high. Try max transmission and longer exposure
                transmission = 100.
                experiment_time = use_dose / std_dose_rate
                exposure_time = experiment_time * image_width / total_strategy_length
            max_exposure = exposure_limits[1]
            if max_exposure and exposure_time > max_exposure:
                # exposure_time over max; set dose to highest achievable dose
                experiment_time = (
                    max_exposure
                    * total_strategy_length
                    / image_width
                )
                use_dose = (std_dose_rate * experiment_time)
                values_map.set_values(
                    exposure=max_exposure,
                    transmission=100,
                    use_dose=use_dose,
                    experiment_time=experiment_time,
                )
            else:
                values_map.set_values(
                    exposure=exposure_time,
                    transmission=transmission,
                    experiment_time=experiment_time,
                )

    @staticmethod
    def update_resolution(values_map:ui_communication.AbstractValuesMap):
        print ('@~@~ executing update_resolution')

        parameters = values_map.get_values_map()
        resolution = float(parameters.get("resolution"))
        decay_limit = float(parameters.get("decay_limit", 0))
        relative_rad_sensitivity = float(parameters.get("relative_rad_sensitivity", 0))
        maximum_dose_budget = float(parameters.get("maximum_dose_budget", 0))
        default_exposure = HWR.beamline.get_default_acquisition_parameters().exp_time
        dbg = 2 * resolution * resolution * math.log(100.0 / decay_limit)
        #
        dbg =  min(dbg, maximum_dose_budget) / relative_rad_sensitivity
        characterisation_budget_fraction = float(parameters.get(
            "characterisation_budget_fraction", 0)
        )
        characterisation_dose =  float(parameters.get("characterisation_dose", 0))
        if characterisation_dose:
            use_dose = dbg - characterisation_dose
        else:
            use_dose = dbg * characterisation_budget_fraction
        values_map.set_values(
            dose_budget=dbg, use_dose=use_dose, exposure=default_exposure
        )
        GphlWorkflow.update_dose(values_map)
