# Registered recipe block types

Generated from `app/recipes/block_registry.py`. `id` identifies an instance; `block_type` identifies its permanent schema. Tree order is not an identifier.

| Block type | YAML type | Device binding | Operation fields |
| --- | --- | --- | --- |
| device.anritsu | sequence | anritsu | acquire_single, acquisition_average_count, acquisition_reference_operation, channel, configuration, configuration_required, description, device_module, disabled, label, managed_acquisition_id, operation, output_policy, parameter_actions, post_configuration_operation, roi_required, source_mode, text, trace |
| device.anritsu_sg | sequence | anritsu_sg | acquire_single, acquisition_average_count, acquisition_reference_operation, channel, configuration, configuration_required, description, device_module, disabled, label, managed_acquisition_id, operation, output_policy, parameter_actions, post_configuration_operation, roi_required, source_mode, text, trace |
| device.keithley | sequence | keithley | acquire_single, acquisition_average_count, acquisition_reference_operation, channel, configuration, configuration_required, description, device_module, disabled, label, managed_acquisition_id, operation, output_policy, parameter_actions, post_configuration_operation, roi_required, source_mode, text, trace |
| device.moke_box | sequence | moke_box | acquire_single, acquisition_average_count, acquisition_reference_operation, channel, configuration, configuration_required, description, device_module, disabled, label, managed_acquisition_id, operation, output_policy, parameter_actions, post_configuration_operation, roi_required, source_mode, text, trace |
| device.rigol | sequence | rigol | acquire_single, acquisition_average_count, acquisition_reference_operation, channel, configuration, configuration_required, description, device_module, disabled, label, managed_acquisition_id, operation, output_policy, parameter_actions, post_configuration_operation, roi_required, source_mode, text, trace |
| recipe.acquire_reference | acquire_reference |  | average_count, description, disabled, file_kind, inter_sweep_delay, label, minimum_duration, purpose, source_file, trace |
| recipe.acquire_spectrum | acquire_spectrum |  | average_count, description, disabled, inter_sweep_delay, label, processing, reference_operation, store_processed, store_raw, trace |
| recipe.arm_moke_voltage | arm_moke_voltage |  | description, disabled, label |
| recipe.checkpoint | checkpoint |  | description, disabled, label |
| recipe.comment | comment |  | comment, description, disabled, label, text |
| recipe.configure_anritsu | configure_anritsu |  | description, disabled, label, points, reference_level, start_frequency, stop_frequency, trace, vbw_filter_mode |
| recipe.configure_anritsu_advanced | configure_anritsu_advanced |  | attenuation, attenuation_mode, description, detector, disabled, label, preamplifier_enabled, rbw, rbw_mode, sweep_time, sweep_time_mode, vbw, vbw_filter_mode, vbw_mode |
| recipe.configure_anritsu_sg | configure_anritsu_sg |  | description, disabled, frequency, label, power |
| recipe.configure_keithley | configure_keithley |  | channel, compliance, description, disabled, label, level, measure_current_autorange, measure_current_range, measure_voltage_autorange, measure_voltage_range, mode, nplc, sense_mode, settle_time, settling_time, source_autorange, source_range |
| recipe.configure_moke_box | configure_moke_box |  | calibration_id, channel, description, disabled, label, maximum_voltage, minimum_voltage |
| recipe.configure_rigol | configure_rigol |  | channel, description, disabled, dut_min_impedance, frequency, high_level, label, low_level, output_load, phase_deg, pulse_leading, pulse_trailing, pulse_width, ramp_symmetry_percent, square_duty_percent, waveform |
| recipe.configure_rigol_output | configure_rigol_output |  | channel, description, disabled, gate_polarity, label, mode, output_load, polarity, sync_delay, sync_enabled, sync_polarity |
| recipe.connect | connect |  | description, device, disabled, label |
| recipe.enable_anritsu_sg_output | enable_anritsu_sg_output |  | description, disabled, label |
| recipe.enable_rigol_output | enable_rigol_output |  | channel, description, disabled, label |
| recipe.if | if |  | condition, description, disabled, label, left, operator, right |
| recipe.measure_keithley | measure_keithley |  | channel, description, disabled, label |
| recipe.measure_lakeshore_field | measure_lakeshore_field |  | checkpoint, description, disabled, label |
| recipe.measure_moke_hall | measure_moke_hall |  | checkpoint, description, disabled, label |
| recipe.ramp_keithley_to_zero | ramp_keithley_to_zero |  | channel, deadline, description, disabled, label |
| recipe.repeat | repeat |  | count, description, disabled, label |
| recipe.sequence | sequence |  | acquire_single, acquisition_average_count, acquisition_reference_operation, channel, configuration, configuration_required, description, device_module, disabled, label, managed_acquisition_id, operation, output_policy, parameter_actions, post_configuration_operation, roi_required, source_mode, text, trace |
| recipe.set_anritsu_sg_output | set_anritsu_sg_output |  | description, disabled, enabled, label |
| recipe.set_keithley_output | set_keithley_output |  | channel, description, disabled, enabled, label |
| recipe.set_moke_voltage | set_moke_voltage |  | channel, description, disabled, label, voltage |
| recipe.set_rigol_output | set_rigol_output |  | channel, description, disabled, enabled, label |
| recipe.stop_moke_voltage | stop_moke_voltage |  | channel, description, disabled, label |
| recipe.final_state | final_state |  | channel, description, device, disabled, frequency, high_level, label, level, low_level, output, voltage |
| recipe.sweep | sweep |  | binding, description, disabled, label, points, segments, spacing, start, stop, target |
| recipe.update_anritsu_sg | update_anritsu_sg |  | description, disabled, frequency, label, power |
| recipe.update_keithley_compliance | update_keithley_compliance |  | channel, compliance, description, disabled, label, mode |
| recipe.update_keithley_level | update_keithley_level |  | channel, description, disabled, label, level, mode |
| recipe.update_moke_voltage | update_moke_voltage |  | channel, description, disabled, label, voltage |
| recipe.update_rigol_frequency | update_rigol_frequency |  | channel, description, disabled, frequency, label |
| recipe.update_rigol_levels | update_rigol_levels |  | channel, description, disabled, high_level, label, low_level |
| recipe.upload_elab | upload_elab |  | attach_csv, attach_hdf5, description, disabled, label, tags, template_id, template_name, title_pattern |
| recipe.upload_to_elab | upload_to_elab |  | attach_csv, attach_hdf5, description, disabled, label, tags, template_id, template_name, title_pattern |
| recipe.wait | wait |  | description, disabled, duration, label |
