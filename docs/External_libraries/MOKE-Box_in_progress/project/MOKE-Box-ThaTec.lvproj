<?xml version='1.0' encoding='UTF-8'?>
<Project Type="Project" LVVersion="15008000">
	<Item Name="My Computer" Type="My Computer">
		<Property Name="NI.SortType" Type="Int">3</Property>
		<Property Name="server.app.propertiesEnabled" Type="Bool">true</Property>
		<Property Name="server.control.propertiesEnabled" Type="Bool">true</Property>
		<Property Name="server.tcp.enabled" Type="Bool">false</Property>
		<Property Name="server.tcp.port" Type="Int">0</Property>
		<Property Name="server.tcp.serviceName" Type="Str">My Computer/VI Server</Property>
		<Property Name="server.tcp.serviceName.default" Type="Str">My Computer/VI Server</Property>
		<Property Name="server.vi.callsEnabled" Type="Bool">true</Property>
		<Property Name="server.vi.propertiesEnabled" Type="Bool">true</Property>
		<Property Name="specify.custom.address" Type="Bool">false</Property>
		<Item Name="low level" Type="Folder">
			<Item Name="lowest level" Type="Folder">
				<Item Name="msb_lsb_to_number.vi" Type="VI" URL="../moke-box-template.llb/msb_lsb_to_number.vi"/>
				<Item Name="number_to_msb_lsb.vi" Type="VI" URL="../moke-box-template.llb/number_to_msb_lsb.vi"/>
				<Item Name="parity check.vi" Type="VI" URL="../moke-box-template.llb/parity check.vi"/>
				<Item Name="Popup.vi" Type="VI" URL="../moke-box-template.llb/Popup.vi"/>
				<Item Name="request_values.vi" Type="VI" URL="../moke-box-template.llb/request_values.vi"/>
				<Item Name="split_command_byte.vi" Type="VI" URL="../moke-box-template.llb/split_command_byte.vi"/>
				<Item Name="error_anzeige.vi" Type="VI" URL="../moke-box-template.llb/error_anzeige.vi"/>
				<Item Name="get_data.vi" Type="VI" URL="../moke-box-template.llb/get_data.vi"/>
			</Item>
			<Item Name="readback_VOUT.vi" Type="VI" URL="../moke-box-template.llb/readback_VOUT.vi"/>
			<Item Name="send_command.vi" Type="VI" URL="../moke-box-template.llb/send_command.vi"/>
			<Item Name="set_Hall_gains.vi" Type="VI" URL="../moke-box-template.llb/set_Hall_gains.vi"/>
			<Item Name="set_Kerr0_gain.vi" Type="VI" URL="../moke-box-template.llb/set_Kerr0_gain.vi"/>
			<Item Name="set_Kerr1_gain.vi" Type="VI" URL="../moke-box-template.llb/set_Kerr1_gain.vi"/>
			<Item Name="voltage_to_zero.vi" Type="VI" URL="../moke-box-template.llb/voltage_to_zero.vi"/>
			<Item Name="set_VOUTn_direct.vi" Type="VI" URL="../moke-box-template.llb/set_VOUTn_direct.vi"/>
		</Item>
		<Item Name="1D" Type="Folder">
			<Item Name="apply_fit.vi" Type="VI" URL="../moke-box-template.llb/apply_fit.vi"/>
			<Item Name="Calculate_mField_from_calibration.vi" Type="VI" URL="../moke-box-template.llb/Calculate_mField_from_calibration.vi"/>
			<Item Name="Calibration_Global.vi" Type="VI" URL="../moke-box-template.llb/Calibration_Global.vi"/>
			<Item Name="Control_Polynomial.vi" Type="VI" URL="../moke-box-template.llb/Control_Polynomial.vi"/>
			<Item Name="do_calibration.vi" Type="VI" URL="../moke-box-template.llb/do_calibration.vi"/>
			<Item Name="Hall_Interpolation_Correction.vi" Type="VI" URL="../moke-box-template.llb/Hall_Interpolation_Correction.vi"/>
			<Item Name="Hall_Polynomial.vi" Type="VI" URL="../moke-box-template.llb/Hall_Polynomial.vi"/>
			<Item Name="load_mcal.vi" Type="VI" URL="../moke-box-template.llb/load_mcal.vi"/>
			<Item Name="p-control-step.vi" Type="VI" URL="../moke-box-template.llb/p-control-step.vi"/>
			<Item Name="Polynomial_Fit_Function.vi" Type="VI" URL="../moke-box-template.llb/Polynomial_Fit_Function.vi"/>
			<Item Name="Read_Hall_Voltage.vi" Type="VI" URL="../moke-box-template.llb/Read_Hall_Voltage.vi"/>
			<Item Name="rough_field_set.vi" Type="VI" URL="../moke-box-template.llb/rough_field_set.vi"/>
			<Item Name="set_voltage_mother.vi" Type="VI" URL="../moke-box-template.llb/set_voltage_mother.vi"/>
			<Item Name="wait_for_stable_field.vi" Type="VI" URL="../moke-box-template.llb/wait_for_stable_field.vi"/>
		</Item>
		<Item Name="moke-box-template" Type="Folder">
			<Property Name="NI.SortType" Type="Int">3</Property>
			<Item Name="2D only" Type="Folder">
				<Item Name="Convert_polar_to_cartesian.vi" Type="VI" URL="../moke-box-template.llb/Convert_polar_to_cartesian.vi"/>
				<Item Name="create_sorted_values_from_data_array.vi" Type="VI" URL="../moke-box-template.llb/create_sorted_values_from_data_array.vi"/>
				<Item Name="Convert_voltage_to_field_strength.vi" Type="VI" URL="../moke-box-template.llb/Convert_voltage_to_field_strength.vi"/>
				<Item Name="Set_fields.vi" Type="VI" URL="../moke-box-template.llb/Set_fields.vi"/>
				<Item Name="Set_Fields_mother.vi" Type="VI" URL="../moke-box-template.llb/Set_Fields_mother.vi"/>
				<Item Name="2D_MOKE_V_read_and_convert.vi" Type="VI" URL="../moke-box-template.llb/2D_MOKE_V_read_and_convert.vi"/>
			</Item>
			<Item Name="Connect_to_MOKE_Box.vi" Type="VI" URL="../moke-box-template.llb/Connect_to_MOKE_Box.vi"/>
			<Item Name="Initialize_MOKE_Voltage.vi" Type="VI" URL="../moke-box-template.llb/Initialize_MOKE_Voltage.vi"/>
			<Item Name="V_read_out_channel.vi" Type="VI" URL="../moke-box-template.llb/V_read_out_channel.vi"/>
		</Item>
		<Item Name="Moke-box-ThaTec.vi" Type="VI" URL="../Moke-box-ThaTec.vi"/>
		<Item Name="run-time-menu.rtm" Type="Document" URL="../run-time-menu.rtm"/>
		<Item Name="Icon.ico" Type="Document" URL="../../../build/Icon.ico"/>
		<Item Name="Dependencies" Type="Dependencies">
			<Item Name="user.lib" Type="Folder">
				<Item Name="about.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/about.vi"/>
				<Item Name="change_status.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/change_status.vi"/>
				<Item Name="change_TCP_settings.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/change_TCP_settings.vi"/>
				<Item Name="change_TCP_status.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/change_TCP_status.vi"/>
				<Item Name="CheckPath.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/CheckPath.vi"/>
				<Item Name="client_info_indexing.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/client_info_indexing.vi"/>
				<Item Name="Convert SFNT error to NI error.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/Convert SFNT error to NI error.vi"/>
				<Item Name="create client info.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/create client info.vi"/>
				<Item Name="ctl properties - get.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/ctl properties - get.vi"/>
				<Item Name="ctl properties - set.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/ctl properties - set.vi"/>
				<Item Name="device-device-feedback.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/device-device-feedback.vi"/>
				<Item Name="device_loop_1.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/device_loop_1.vi"/>
				<Item Name="device_loop_2.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/device_loop_2.vi"/>
				<Item Name="disable_ctls.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/disable_ctls.vi"/>
				<Item Name="exit_vi.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/exit_vi.vi"/>
				<Item Name="find_ctl_ref.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/find_ctl_ref.vi"/>
				<Item Name="get-project-name.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/get-project-name.vi"/>
				<Item Name="get_ctrl_info_from_client_info.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/get_ctrl_info_from_client_info.vi"/>
				<Item Name="get_tab_ctrl_info.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/get_tab_ctrl_info.vi"/>
				<Item Name="get_tab_ctrls.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/get_tab_ctrls.vi"/>
				<Item Name="global_variable.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/global_variable.vi"/>
				<Item Name="handle_time-out_values.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/handle_time-out_values.vi"/>
				<Item Name="hasp login.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/hasp login.vi"/>
				<Item Name="hasp logout.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/hasp logout.vi"/>
				<Item Name="initialize rt ctl.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/initialize rt ctl.vi"/>
				<Item Name="interaction_handling.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/interaction_handling.vi"/>
				<Item Name="Load_All_VI_Ctrls.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/Load_All_VI_Ctrls.vi"/>
				<Item Name="load_controls.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/load_controls.vi"/>
				<Item Name="measurement_done.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/measurement_done.vi"/>
				<Item Name="panel_close.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/panel_close.vi"/>
				<Item Name="prepare_client_tree_for_send.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/prepare_client_tree_for_send.vi"/>
				<Item Name="prepare_update_for_server2.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/prepare_update_for_server2.vi"/>
				<Item Name="properties.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/properties.vi"/>
				<Item Name="read_server_config.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/read_server_config.vi"/>
				<Item Name="run-time_menu.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/run-time_menu.vi"/>
				<Item Name="Save_All_VI_Ctrls.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/Save_All_VI_Ctrls.vi"/>
				<Item Name="save_controls.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/save_controls.vi"/>
				<Item Name="save_tab_ctrls.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/save_tab_ctrls.vi"/>
				<Item Name="server_variables.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/server_variables.vi"/>
				<Item Name="SortTree.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/SortTree.vi"/>
				<Item Name="startup.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/startup.vi"/>
				<Item Name="TCP_read.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/TCP_read.vi"/>
				<Item Name="thaTEC-driver_version.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/thaTEC-driver_version.vi"/>
				<Item Name="thaTEC_driver.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/thaTEC_driver.vi"/>
				<Item Name="Tree_from_client_info.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/Tree_from_client_info.vi"/>
				<Item Name="update and feedback.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/update and feedback.vi"/>
				<Item Name="var2ctl-client.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/var2ctl-client.vi"/>
				<Item Name="var2TCP.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/var2TCP.vi"/>
				<Item Name="write_server_conf.vi" Type="VI" URL="/&lt;userlib&gt;/thaTEC-driver/_private/write_server_conf.vi"/>
			</Item>
			<Item Name="vi.lib" Type="Folder">
				<Item Name="Application Directory.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/file.llb/Application Directory.vi"/>
				<Item Name="BuildHelpPath.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/BuildHelpPath.vi"/>
				<Item Name="Check if File or Folder Exists.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/libraryn.llb/Check if File or Folder Exists.vi"/>
				<Item Name="Check Special Tags.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Check Special Tags.vi"/>
				<Item Name="Clear Errors.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Clear Errors.vi"/>
				<Item Name="Close File+.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/file.llb/Close File+.vi"/>
				<Item Name="compatReadText.vi" Type="VI" URL="/&lt;vilib&gt;/_oldvers/_oldvers.llb/compatReadText.vi"/>
				<Item Name="Convert property node font to graphics font.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Convert property node font to graphics font.vi"/>
				<Item Name="Details Display Dialog.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Details Display Dialog.vi"/>
				<Item Name="DialogType.ctl" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/DialogType.ctl"/>
				<Item Name="DialogTypeEnum.ctl" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/DialogTypeEnum.ctl"/>
				<Item Name="Draw Flattened Pixmap.vi" Type="VI" URL="/&lt;vilib&gt;/picture/picture.llb/Draw Flattened Pixmap.vi"/>
				<Item Name="Error Cluster From Error Code.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Error Cluster From Error Code.vi"/>
				<Item Name="Error Code Database.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Error Code Database.vi"/>
				<Item Name="ErrWarn.ctl" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/ErrWarn.ctl"/>
				<Item Name="eventvkey.ctl" Type="VI" URL="/&lt;vilib&gt;/event_ctls.llb/eventvkey.ctl"/>
				<Item Name="ex_CorrectErrorChain.vi" Type="VI" URL="/&lt;vilib&gt;/express/express shared/ex_CorrectErrorChain.vi"/>
				<Item Name="Find First Error.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Find First Error.vi"/>
				<Item Name="Find Tag.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Find Tag.vi"/>
				<Item Name="FixBadRect.vi" Type="VI" URL="/&lt;vilib&gt;/picture/pictutil.llb/FixBadRect.vi"/>
				<Item Name="Format Message String.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Format Message String.vi"/>
				<Item Name="General Error Handler Core CORE.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/General Error Handler Core CORE.vi"/>
				<Item Name="General Error Handler.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/General Error Handler.vi"/>
				<Item Name="Get String Text Bounds.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Get String Text Bounds.vi"/>
				<Item Name="Get System Directory.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/sysdir.llb/Get System Directory.vi"/>
				<Item Name="Get Text Rect.vi" Type="VI" URL="/&lt;vilib&gt;/picture/picture.llb/Get Text Rect.vi"/>
				<Item Name="GetHelpDir.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/GetHelpDir.vi"/>
				<Item Name="GetRTHostConnectedProp.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/GetRTHostConnectedProp.vi"/>
				<Item Name="imagedata.ctl" Type="VI" URL="/&lt;vilib&gt;/picture/picture.llb/imagedata.ctl"/>
				<Item Name="Longest Line Length in Pixels.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Longest Line Length in Pixels.vi"/>
				<Item Name="LVBoundsTypeDef.ctl" Type="VI" URL="/&lt;vilib&gt;/Utility/miscctls.llb/LVBoundsTypeDef.ctl"/>
				<Item Name="LVRectTypeDef.ctl" Type="VI" URL="/&lt;vilib&gt;/Utility/miscctls.llb/LVRectTypeDef.ctl"/>
				<Item Name="NI_AALBase.lvlib" Type="Library" URL="/&lt;vilib&gt;/Analysis/NI_AALBase.lvlib"/>
				<Item Name="NI_AALPro.lvlib" Type="Library" URL="/&lt;vilib&gt;/Analysis/NI_AALPro.lvlib"/>
				<Item Name="NI_Data Type.lvlib" Type="Library" URL="/&lt;vilib&gt;/Utility/Data Type/NI_Data Type.lvlib"/>
				<Item Name="NI_FileType.lvlib" Type="Library" URL="/&lt;vilib&gt;/Utility/lvfile.llb/NI_FileType.lvlib"/>
				<Item Name="NI_Gmath.lvlib" Type="Library" URL="/&lt;vilib&gt;/gmath/NI_Gmath.lvlib"/>
				<Item Name="NI_Matrix.lvlib" Type="Library" URL="/&lt;vilib&gt;/Analysis/Matrix/NI_Matrix.lvlib"/>
				<Item Name="NI_PackedLibraryUtility.lvlib" Type="Library" URL="/&lt;vilib&gt;/Utility/LVLibp/NI_PackedLibraryUtility.lvlib"/>
				<Item Name="Not Found Dialog.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Not Found Dialog.vi"/>
				<Item Name="Open File+.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/file.llb/Open File+.vi"/>
				<Item Name="Read Delimited Spreadsheet (DBL).vi" Type="VI" URL="/&lt;vilib&gt;/Utility/file.llb/Read Delimited Spreadsheet (DBL).vi"/>
				<Item Name="Read Delimited Spreadsheet (I64).vi" Type="VI" URL="/&lt;vilib&gt;/Utility/file.llb/Read Delimited Spreadsheet (I64).vi"/>
				<Item Name="Read Delimited Spreadsheet (string).vi" Type="VI" URL="/&lt;vilib&gt;/Utility/file.llb/Read Delimited Spreadsheet (string).vi"/>
				<Item Name="Read Delimited Spreadsheet.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/file.llb/Read Delimited Spreadsheet.vi"/>
				<Item Name="Read File+ (string).vi" Type="VI" URL="/&lt;vilib&gt;/Utility/file.llb/Read File+ (string).vi"/>
				<Item Name="Read Lines From File (with error IO).vi" Type="VI" URL="/&lt;vilib&gt;/Utility/file.llb/Read Lines From File (with error IO).vi"/>
				<Item Name="Search and Replace Pattern.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Search and Replace Pattern.vi"/>
				<Item Name="Set Bold Text.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Set Bold Text.vi"/>
				<Item Name="Set String Value.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Set String Value.vi"/>
				<Item Name="Simple Error Handler.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Simple Error Handler.vi"/>
				<Item Name="Space Constant.vi" Type="VI" URL="/&lt;vilib&gt;/dlg_ctls.llb/Space Constant.vi"/>
				<Item Name="subDisplayMessage.vi" Type="VI" URL="/&lt;vilib&gt;/express/express output/DisplayMessageBlock.llb/subDisplayMessage.vi"/>
				<Item Name="subFile Dialog.vi" Type="VI" URL="/&lt;vilib&gt;/express/express input/FileDialogBlock.llb/subFile Dialog.vi"/>
				<Item Name="System Directory Type.ctl" Type="VI" URL="/&lt;vilib&gt;/Utility/sysdir.llb/System Directory Type.ctl"/>
				<Item Name="TagReturnType.ctl" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/TagReturnType.ctl"/>
				<Item Name="Three Button Dialog CORE.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Three Button Dialog CORE.vi"/>
				<Item Name="Three Button Dialog.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Three Button Dialog.vi"/>
				<Item Name="Trim Whitespace.vi" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/Trim Whitespace.vi"/>
				<Item Name="whitespace.ctl" Type="VI" URL="/&lt;vilib&gt;/Utility/error.llb/whitespace.ctl"/>
			</Item>
			<Item Name="lvanlys.dll" Type="Document" URL="/&lt;resource&gt;/lvanlys.dll"/>
		</Item>
		<Item Name="Build Specifications" Type="Build">
			<Item Name="Moke-box-ThaTec" Type="EXE">
				<Property Name="App_copyErrors" Type="Bool">true</Property>
				<Property Name="App_INI_aliasGUID" Type="Str">{3DFA1314-2C0A-425E-AA04-62A61D57848E}</Property>
				<Property Name="App_INI_GUID" Type="Str">{1C2B1455-DE44-40C4-8136-21A8A9ED4B86}</Property>
				<Property Name="App_serverConfig.httpPort" Type="Int">8002</Property>
				<Property Name="Bld_autoIncrement" Type="Bool">true</Property>
				<Property Name="Bld_buildCacheID" Type="Str">{4AE259EA-D367-4022-BD87-1668E24019B9}</Property>
				<Property Name="Bld_buildSpecName" Type="Str">Moke-box-ThaTec</Property>
				<Property Name="Bld_localDestDir" Type="Path">/C/Users/BLS2/Desktop/moke-box/build_2021-12</Property>
				<Property Name="Bld_previewCacheID" Type="Str">{4CD15EEB-46F7-48C2-A516-4A1535BED322}</Property>
				<Property Name="Bld_version.build" Type="Int">5</Property>
				<Property Name="Bld_version.major" Type="Int">1</Property>
				<Property Name="Destination[0].destName" Type="Str">Moke-Box-Thatec_Bls2.exe</Property>
				<Property Name="Destination[0].path" Type="Path">/C/Users/BLS2/Desktop/moke-box/build_2021-12/Moke-Box-Thatec_Bls2.exe</Property>
				<Property Name="Destination[0].path.type" Type="Str">&lt;none&gt;</Property>
				<Property Name="Destination[0].preserveHierarchy" Type="Bool">true</Property>
				<Property Name="Destination[0].type" Type="Str">App</Property>
				<Property Name="Destination[1].destName" Type="Str">Support Directory</Property>
				<Property Name="Destination[1].path" Type="Path">/C/Users/BLS2/Desktop/moke-box/build_2021-12/data</Property>
				<Property Name="Destination[1].path.type" Type="Str">&lt;none&gt;</Property>
				<Property Name="DestinationCount" Type="Int">2</Property>
				<Property Name="Exe_iconItemID" Type="Ref">/My Computer/Icon.ico</Property>
				<Property Name="Source[0].itemID" Type="Str">{6EBC7C1F-9B7F-46C7-BF7B-A8079ABFC2DA}</Property>
				<Property Name="Source[0].type" Type="Str">Container</Property>
				<Property Name="Source[1].destinationIndex" Type="Int">0</Property>
				<Property Name="Source[1].itemID" Type="Ref">/My Computer/Moke-box-ThaTec.vi</Property>
				<Property Name="Source[1].sourceInclusion" Type="Str">TopLevel</Property>
				<Property Name="Source[1].type" Type="Str">VI</Property>
				<Property Name="Source[2].Container.applyInclusion" Type="Bool">true</Property>
				<Property Name="Source[2].Container.depDestIndex" Type="Int">0</Property>
				<Property Name="Source[2].destinationIndex" Type="Int">0</Property>
				<Property Name="Source[2].itemID" Type="Ref">/My Computer/low level</Property>
				<Property Name="Source[2].sourceInclusion" Type="Str">Include</Property>
				<Property Name="Source[2].type" Type="Str">Container</Property>
				<Property Name="Source[3].Container.applyInclusion" Type="Bool">true</Property>
				<Property Name="Source[3].Container.depDestIndex" Type="Int">0</Property>
				<Property Name="Source[3].destinationIndex" Type="Int">0</Property>
				<Property Name="Source[3].itemID" Type="Ref">/My Computer/1D</Property>
				<Property Name="Source[3].sourceInclusion" Type="Str">Include</Property>
				<Property Name="Source[3].type" Type="Str">Container</Property>
				<Property Name="Source[4].Container.applyInclusion" Type="Bool">true</Property>
				<Property Name="Source[4].Container.depDestIndex" Type="Int">0</Property>
				<Property Name="Source[4].destinationIndex" Type="Int">0</Property>
				<Property Name="Source[4].itemID" Type="Ref">/My Computer/moke-box-template</Property>
				<Property Name="Source[4].sourceInclusion" Type="Str">Include</Property>
				<Property Name="Source[4].type" Type="Str">Container</Property>
				<Property Name="Source[5].destinationIndex" Type="Int">0</Property>
				<Property Name="Source[5].itemID" Type="Ref">/My Computer/run-time-menu.rtm</Property>
				<Property Name="Source[5].lvfile" Type="Bool">true</Property>
				<Property Name="Source[5].sourceInclusion" Type="Str">Include</Property>
				<Property Name="SourceCount" Type="Int">6</Property>
				<Property Name="TgtF_fileDescription" Type="Str">Moke-box-ThaTec</Property>
				<Property Name="TgtF_internalName" Type="Str">Moke-box-ThaTec</Property>
				<Property Name="TgtF_legalCopyright" Type="Str">Copyright © 2019 </Property>
				<Property Name="TgtF_productName" Type="Str">Moke-box-ThaTec</Property>
				<Property Name="TgtF_targetfileGUID" Type="Str">{ECD4434A-7561-4974-B5A4-295097E9781A}</Property>
				<Property Name="TgtF_targetfileName" Type="Str">Moke-Box-Thatec_Bls2.exe</Property>
			</Item>
		</Item>
	</Item>
</Project>
