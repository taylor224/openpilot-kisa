from collections import deque
from cereal import car, log
import cereal.messaging as messaging
from opendbc.car import DT_CTRL, structs
from opendbc.car.interfaces import MAX_CTRL_SPEED
from opendbc.car.volkswagen.values import CarControllerParams as VWCarControllerParams
from opendbc.car.hyundai.interface import ENABLE_BUTTONS as HYUNDAI_ENABLE_BUTTONS
from opendbc.car.hyundai.carstate import PREV_BUTTON_SAMPLES as HYUNDAI_PREV_BUTTON_SAMPLES

from openpilot.selfdrive.selfdrived.events import Events

from openpilot.common.params import Params

ButtonType = structs.CarState.ButtonEvent.Type
GearShifter = structs.CarState.GearShifter
EventName = log.OnroadEvent.EventName
NetworkLocation = structs.CarParams.NetworkLocation


# TODO: the goal is to abstract this file into the CarState struct and make events generic
class MockCarState:
  def __init__(self):
    self.sm = messaging.SubMaster(['gpsLocation', 'gpsLocationExternal'])

  def update(self, CS: car.CarState):
    self.sm.update(0)
    gps_sock = 'gpsLocationExternal' if self.sm.recv_frame['gpsLocationExternal'] > 1 else 'gpsLocation'

    CS.vEgo = self.sm[gps_sock].speed
    CS.vEgoRaw = self.sm[gps_sock].speed

    return CS


class CarSpecificEvents:
  def __init__(self, CP: structs.CarParams):
    self.CP = CP

    self.steering_unpressed = 0
    self.low_speed_alert = False
    self.no_steer_warning = False
    self.silent_steer_warning = True

    self.cruise_buttons: deque = deque([], maxlen=HYUNDAI_PREV_BUTTON_SAMPLES)

    self.ufc_mode = Params().get_bool("UFCModeEnabled")
    self.steer_warning_fix_enabled = Params().get_bool("SteerWarningFix")
    self.user_specific_feature = int(Params().get("UserSpecificFeature", encoding="utf8"))
    self.long_alt = int(Params().get("KISALongAlt", encoding="utf8"))
    self.exp_long = self.CP.sccBus <= 0 and self.CP.openpilotLongitudinalControl and self.long_alt not in (1, 2)
    self.no_mdps_mods = Params().get_bool("NoSmartMDPS")

  def update(self, CS: car.CarState, CS_prev: car.CarState, CC: car.CarControl):
    if self.CP.carName in ('body', 'mock'):
      events = Events()

    elif self.CP.carName in ('subaru', 'mazda'):
      events = self.create_common_events(CS, CS_prev)

    elif self.CP.carName == 'ford':
      events = self.create_common_events(CS, CS_prev, extra_gears=[GearShifter.manumatic])

    elif self.CP.carName == 'nissan':
      events = self.create_common_events(CS, CS_prev, extra_gears=[GearShifter.brake])

    elif self.CP.carName == 'chrysler':
      events = self.create_common_events(CS, CS_prev, extra_gears=[GearShifter.low])

      # Low speed steer alert hysteresis logic
      if self.CP.minSteerSpeed > 0. and CS.vEgo < (self.CP.minSteerSpeed + 0.5):
        self.low_speed_alert = True
      elif CS.vEgo > (self.CP.minSteerSpeed + 1.):
        self.low_speed_alert = False
      if self.low_speed_alert:
        events.add(EventName.belowSteerSpeed)

    elif self.CP.carName == 'honda':
      events = self.create_common_events(CS, CS_prev, pcm_enable=False)

      if self.CP.pcmCruise and CS.vEgo < self.CP.minEnableSpeed:
        events.add(EventName.belowEngageSpeed)

      if self.CP.pcmCruise:
        # we engage when pcm is active (rising edge)
        if CS.cruiseState.enabled and not CS_prev.cruiseState.enabled:
          events.add(EventName.pcmEnable)
        elif not CS.cruiseState.enabled and (CC.actuators.accel >= 0. or not self.CP.openpilotLongitudinalControl):
          # it can happen that car cruise disables while comma system is enabled: need to
          # keep braking if needed or if the speed is very low
          if CS.vEgo < self.CP.minEnableSpeed + 2.:
            # non loud alert if cruise disables below 25mph as expected (+ a little margin)
            events.add(EventName.speedTooLow)
          else:
            events.add(EventName.cruiseDisabled)
      if self.CP.minEnableSpeed > 0 and CS.vEgo < 0.001:
        events.add(EventName.manualRestart)

    elif self.CP.carName == 'toyota':
      events = self.create_common_events(CS, CS_prev)

      if self.CP.openpilotLongitudinalControl:
        if CS.cruiseState.standstill and not CS.brakePressed:
          events.add(EventName.resumeRequired)
        if CS.vEgo < self.CP.minEnableSpeed:
          events.add(EventName.belowEngageSpeed)
          if CC.actuators.accel > 0.3:
            # some margin on the actuator to not false trigger cancellation while stopping
            events.add(EventName.speedTooLow)
          if CS.vEgo < 0.001:
            # while in standstill, send a user alert
            events.add(EventName.manualRestart)

    elif self.CP.carName == 'gm':
      # The ECM allows enabling on falling edge of set, but only rising edge of resume
      events = self.create_common_events(CS, CS_prev, extra_gears=[GearShifter.sport, GearShifter.low,
                                                                   GearShifter.eco, GearShifter.manumatic],
                                         pcm_enable=self.CP.pcmCruise, enable_buttons=(ButtonType.decelCruise,))
      if not self.CP.pcmCruise:
        if any(b.type == ButtonType.accelCruise and b.pressed for b in CS.buttonEvents):
          events.add(EventName.buttonEnable)

      # Enabling at a standstill with brake is allowed
      # TODO: verify 17 Volt can enable for the first time at a stop and allow for all GMs
      if CS.vEgo < self.CP.minEnableSpeed and not (CS.standstill and CS.brake >= 20 and
                                                   self.CP.networkLocation == NetworkLocation.fwdCamera):
        events.add(EventName.belowEngageSpeed)
      if CS.cruiseState.standstill:
        events.add(EventName.resumeRequired)
      if CS.vEgo < self.CP.minSteerSpeed:
        events.add(EventName.belowSteerSpeed)

    elif self.CP.carName == 'volkswagen':
      events = self.create_common_events(CS, CS_prev, extra_gears=[GearShifter.eco, GearShifter.sport, GearShifter.manumatic],
                                         pcm_enable=not self.CP.openpilotLongitudinalControl,
                                         enable_buttons=(ButtonType.setCruise, ButtonType.resumeCruise))

      # Low speed steer alert hysteresis logic
      if (self.CP.minSteerSpeed - 1e-3) > VWCarControllerParams.DEFAULT_MIN_STEER_SPEED and CS.vEgo < (self.CP.minSteerSpeed + 1.):
        self.low_speed_alert = True
      elif CS.vEgo > (self.CP.minSteerSpeed + 2.):
        self.low_speed_alert = False
      if self.low_speed_alert:
        events.add(EventName.belowSteerSpeed)

      if self.CP.openpilotLongitudinalControl:
        if CS.vEgo < self.CP.minEnableSpeed + 0.5:
          events.add(EventName.belowEngageSpeed)
        if CC.enabled and CS.vEgo < self.CP.minEnableSpeed:
          events.add(EventName.speedTooLow)

      # TODO: this needs to be implemented generically in carState struct
      # if CC.eps_timer_soft_disable_alert:  # type: ignore[attr-defined]
      #   events.add(EventName.steerTimeLimit)

    elif self.CP.carName == 'hyundai':
      # On some newer model years, the CANCEL button acts as a pause/resume button based on the PCM state
      # To avoid re-engaging when openpilot cancels, check user engagement intention via buttons
      # Main button also can trigger an engagement on these cars
      self.cruise_buttons.append(any(ev.type in HYUNDAI_ENABLE_BUTTONS for ev in CS.buttonEvents))
      events = self.create_common_events(CS, CS_prev, pcm_enable=self.CP.pcmCruise, allow_enable=any(self.cruise_buttons))

      # low speed steer alert hysteresis logic (only for cars with steer cut off above 10 m/s)
      if CS.vEgo < (self.CP.minSteerSpeed + 2.) and self.CP.minSteerSpeed > 10.:
        self.low_speed_alert = True
      if CS.vEgo > (self.CP.minSteerSpeed + 4.):
        self.low_speed_alert = False
      if self.low_speed_alert and self.no_mdps_mods:
        events.add(EventName.belowSteerSpeed)

      # kisa
      if CC.actuators.needBrake and not self.CP.openpilotLongitudinalControl:
        events.add(EventName.needBrake)
      if not CC.actuators.lkasTempDisabled:
        if CC.actuators.lanechangeManualTimer and CS.vEgo > 0.3:
          events.add(EventName.laneChangeManual)
        if CC.actuators.emergencyManualTimer:
          events.add(EventName.emgButtonManual)
        if CC.actuators.standstillResButton:
          events.add(EventName.standstillResButton)
        if CC.actuators.cruiseGapAdjusting:
          events.add(EventName.gapAdjusting)
        if CC.actuators.onSpeedBumpControl and CS.vEgo > 8.3:
          events.add(EventName.speedBump)
        if CC.actuators.onSpeedControl and CS.vEgo > 0.3:
          events.add(EventName.camSpeedDown)
        if CC.actuators.curvSpeedControl and CS.vEgo > 8.3:
          events.add(EventName.curvSpeedDown)
        if CC.actuators.cutInControl and CS.vEgo > 8.3:
          events.add(EventName.cutinDetection)
        if CC.actuators.driverSccSetControl:
          events.add(EventName.sccDriverOverride)        
        if CC.actuators.autoholdPopupTimer:
          events.add(EventName.autoHold)
        if CC.actuators.autoResStarting:
          events.add(EventName.resCruise)
        if CC.actuators.e2eStandstill:
          events.add(EventName.chimeAtResume)

      if CC.actuators.modeChangeTimer:
        if CS.cruiseState.modeSel == 0:
          events.add(EventName.modeChangeOpenpilot)
        elif CS.cruiseState.modeSel == 1:
          events.add(EventName.modeChangeDistcurv)
        elif CS.cruiseState.modeSel == 2:
          events.add(EventName.modeChangeDistance)
        elif CS.cruiseState.modeSel == 3:
          events.add(EventName.modeChangeCurv)
        elif CS.cruiseState.modeSel == 4:
          events.add(EventName.modeChangeOneway)
        elif CS.cruiseState.modeSel == 5:
          events.add(EventName.modeChangeMaponly)

      if CC.actuators.lkasTempDisabled:
        events.add(EventName.lkasDisabled)
      elif CC.actuators.lkasTempDisabledTimer:
        events.add(EventName.lkasEnabled)

      if self.exp_long:
        if CS.cruiseState.enabled and not CS_prev.cruiseState.enabled and any(self.cruise_buttons):
          events.add(EventName.buttonEnable)
        elif not CS.cruiseState.enabled and any(self.cruise_buttons):
          events.add(EventName.buttonCancel)

    else:
      raise ValueError(f"Unsupported car: {self.CP.carName}")

    return events

  def create_common_events(self, CS: structs.CarState, CS_prev: car.CarState, extra_gears=None, pcm_enable=True,
                           allow_enable=True, enable_buttons=(ButtonType.accelCruise, ButtonType.decelCruise)):
    events = Events()

    if self.user_specific_feature == 11:
      if CS.gearShifter != GearShifter.drive and (extra_gears is None or
        CS.gearShifter not in extra_gears) and CS.cruiseState.enabled:
        events.add(EventName.gearNotD)
      if CS.gearShifter == GearShifter.reverse:
        events.add(EventName.reverseGear)
    else:
      if CS.doorOpen:
        events.add(EventName.doorOpen)
      if CS.seatbeltUnlatched:
        events.add(EventName.seatbeltNotLatched)
      if CS.gearShifter != GearShifter.drive and (extra_gears is None or
        CS.gearShifter not in extra_gears):
        events.add(EventName.wrongGear)
      if CS.gearShifter == GearShifter.reverse:
        events.add(EventName.reverseGear)
      if not CS.cruiseState.available:
        events.add(EventName.wrongCarMode)
    if CS.espDisabled:
      events.add(EventName.espDisabled)
    if CS.espActive:
      events.add(EventName.espActive)
    if CS.stockFcw:
      events.add(EventName.stockFcw)
    if CS.stockAeb:
      events.add(EventName.stockAeb)
    if CS.vEgo > MAX_CTRL_SPEED:
      events.add(EventName.speedTooHigh)
    if CS.cruiseState.nonAdaptive:
      events.add(EventName.wrongCruiseMode)
    if CS.brakeHoldActive and self.CP.openpilotLongitudinalControl and not self.ufc_mode:
      events.add(EventName.brakeHold)
    if CS.parkingBrake and not self.ufc_mode:
      events.add(EventName.parkBrake)
    if CS.accFaulted:
      events.add(EventName.accFaulted)
    if CS.steeringPressed:
      events.add(EventName.steerOverride)
    if CS.brakePressed and CS.standstill and not self.ufc_mode:
      events.add(EventName.preEnableStandstill)
    if CS.gasPressed and not self.ufc_mode:
      events.add(EventName.gasPressedOverride)
    if CS.vehicleSensorsInvalid:
      events.add(EventName.vehicleSensorsInvalid)
    if CS.invalidLkasSetting:
      events.add(EventName.invalidLkasSetting)
    if CS.lowSpeedAlert and self.no_mdps_mods:
      events.add(EventName.belowSteerSpeed)

    # Handle button presses
    for b in CS.buttonEvents:
      # Enable OP long on falling edge of enable buttons (defaults to accelCruise and decelCruise, overridable per-port)
      if not self.CP.pcmCruise and (b.type in enable_buttons and not b.pressed):
        events.add(EventName.buttonEnable)
      # Disable on rising and falling edge of cancel for both stock and OP long
      if b.type == ButtonType.cancel and not self.ufc_mode:
        events.add(EventName.buttonCancel)

    # Handle permanent and temporary steering faults
    self.steering_unpressed = 0 if CS.steeringPressed else self.steering_unpressed + 1
    if CS.steerFaultTemporary and not self.steer_warning_fix_enabled:
      if (CS.vEgo < 0.1 or CS.standstill) and CS.steeringAngleDeg < 90:
        events.add(EventName.isgActive)
      elif CS.steeringPressed and (not CS_prev.steerFaultTemporary or self.no_steer_warning):
        self.no_steer_warning = True
      else:
        self.no_steer_warning = False

        # if the user overrode recently, show a less harsh alert
        if self.silent_steer_warning or CS.standstill or self.steering_unpressed < int(1.5 / DT_CTRL):
          self.silent_steer_warning = True
          events.add(EventName.steerTempUnavailableSilent)
        else:
          events.add(EventName.steerTempUnavailable)
    else:
      self.no_steer_warning = False
      self.silent_steer_warning = False
    if CS.steerFaultPermanent:
      events.add(EventName.steerUnavailable)

    # we engage when pcm is active (rising edge)
    # enabling can optionally be blocked by the car interface
    if pcm_enable:
      if CS.cruiseState.enabled and not CS_prev.cruiseState.enabled and allow_enable:
        events.add(EventName.pcmEnable)
      elif not CS.cruiseState.enabled:
        events.add(EventName.pcmDisable)

    return events