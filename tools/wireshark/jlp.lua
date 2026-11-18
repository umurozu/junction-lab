-- Wireshark dissector for JLP (Junction Link Protocol)
-- Install: copy to the Wireshark personal plugins folder
--   (Help > About Wireshark > Folders > Personal Lua Plugins), restart Wireshark.
-- Decodes UDP 15050 (center) and 6001-6010 (devices) automatically.
-- SNMP on the non-standard ports: Analyze > Decode As... > UDP 16101 -> SNMP.

local jlp = Proto("jlp", "Junction Link Protocol")

local types = { [0x01]="STATUS", [0x02]="DETECTOR", [0x03]="ALARM", [0x04]="COUNTS",
                [0x10]="COMMAND", [0x11]="ACK" }
local modes = { [1]="FIXED", [2]="ACTUATED", [3]="FLASH", [4]="STARTUP" }
local intervals = { [1]="GREEN", [2]="YELLOW", [3]="ALL_RED", [4]="RED_YELLOW", [5]="FLASH", [6]="STARTUP" }
local commands = { [1]="SET_PATTERN", [2]="PREEMPT", [3]="INJECT_FAULT", [4]="CLEAR_FAULTS", [5]="PED_REQUEST" }

local f = jlp.fields
f.magic   = ProtoField.string("jlp.magic", "Magic")
f.version = ProtoField.uint8("jlp.version", "Version")
f.type    = ProtoField.uint8("jlp.type", "Type", base.HEX, types)
f.seq     = ProtoField.uint16("jlp.seq", "Sequence")
f.device  = ProtoField.uint16("jlp.device", "Device ID")
f.length  = ProtoField.uint16("jlp.length", "Payload length")
f.crc     = ProtoField.uint16("jlp.crc", "CRC16", base.HEX)
f.uptime  = ProtoField.uint32("jlp.uptime", "Uptime (s)")
f.mode    = ProtoField.uint8("jlp.mode", "Mode", base.DEC, modes)
f.pattern = ProtoField.uint8("jlp.pattern", "Pattern")
f.stage   = ProtoField.uint8("jlp.stage", "Stage")
f.interval= ProtoField.uint8("jlp.interval", "Interval", base.DEC, intervals)
f.greens  = ProtoField.uint16("jlp.greens", "Greens", base.HEX)
f.yellows = ProtoField.uint16("jlp.yellows", "Yellows", base.HEX)
f.reds    = ProtoField.uint16("jlp.reds", "Reds", base.HEX)
f.ry      = ProtoField.uint16("jlp.red_yellows", "Red+Yellows", base.HEX)
f.walks   = ProtoField.uint16("jlp.walks", "Walks", base.HEX)
f.alarms  = ProtoField.uint16("jlp.alarms", "Alarms", base.HEX)
f.cycle   = ProtoField.uint16("jlp.cycle", "Cycle")
f.phase   = ProtoField.uint8("jlp.phase", "Phase")
f.count   = ProtoField.uint8("jlp.count", "Count")
f.presence= ProtoField.bool("jlp.presence", "Presence")
f.command = ProtoField.uint8("jlp.command", "Command", base.DEC, commands)
f.arg     = ProtoField.uint16("jlp.arg", "Argument")
f.arg2    = ProtoField.uint16("jlp.arg2", "Argument 2")
f.ok      = ProtoField.bool("jlp.ok", "OK")
f.volume  = ProtoField.uint32("jlp.volume", "Volume")

local function crc16(tvb, len)
  local crc = 0xFFFF
  for i = 0, len - 1 do
    crc = bit.bxor(crc, bit.lshift(tvb(i, 1):uint(), 8))
    for _ = 1, 8 do
      if bit.band(crc, 0x8000) ~= 0 then crc = bit.band(bit.bxor(bit.lshift(crc, 1), 0x1021), 0xFFFF)
      else crc = bit.band(bit.lshift(crc, 1), 0xFFFF) end
    end
  end
  return crc
end

function jlp.dissector(tvb, pinfo, tree)
  if tvb:len() < 12 or tvb(0, 2):string() ~= "JL" then return 0 end
  pinfo.cols.protocol = "JLP"
  local t = tvb(3, 1):uint()
  local len = tvb(8, 2):uint()
  local sub = tree:add(jlp, tvb(0, 12 + len), "Junction Link Protocol")
  sub:add(f.magic, tvb(0, 2)); sub:add(f.version, tvb(2, 1)); sub:add(f.type, tvb(3, 1))
  sub:add(f.seq, tvb(4, 2)); sub:add(f.device, tvb(6, 2)); sub:add(f.length, tvb(8, 2))
  local p = tvb(10, len)
  local info = (types[t] or "?") .. " dev=" .. tvb(6, 2):uint() .. " seq=" .. tvb(4, 2):uint()
  if t == 0x01 and len >= 22 then
    sub:add(f.uptime, p(0, 4)); sub:add(f.mode, p(4, 1)); sub:add(f.pattern, p(5, 1)); sub:add(f.stage, p(6, 1))
    sub:add(f.interval, p(7, 1)); sub:add(f.greens, p(8, 2)); sub:add(f.yellows, p(10, 2)); sub:add(f.reds, p(12, 2))
    sub:add(f.ry, p(14, 2)); sub:add(f.walks, p(16, 2)); sub:add(f.alarms, p(18, 2)); sub:add(f.cycle, p(20, 2))
    info = info .. " " .. (modes[p(4, 1):uint()] or "?") .. " stage=" .. p(6, 1):uint()
  elseif t == 0x02 and len >= 3 then
    sub:add(f.phase, p(0, 1)); sub:add(f.count, p(1, 1)); sub:add(f.presence, p(2, 1))
    info = info .. " phase=" .. p(0, 1):uint() .. " count=" .. p(1, 1):uint()
  elseif t == 0x03 and len >= 4 then
    sub:add(f.alarms, p(0, 2)); sub:add(f.phase, p(2, 1)); sub:add(f.ok, p(3, 1))
  elseif t == 0x04 and len >= 1 then
    for i = 0, p(0, 1):uint() - 1 do
      local row = sub:add(p(1 + i * 5, 5), "Phase " .. p(1 + i * 5, 1):uint())
      row:add(f.phase, p(1 + i * 5, 1)); row:add(f.volume, p(2 + i * 5, 4))
    end
  elseif t == 0x10 and len >= 5 then
    sub:add(f.command, p(0, 1)); sub:add(f.arg, p(1, 2)); sub:add(f.arg2, p(3, 2))
    info = info .. " " .. (commands[p(0, 1):uint()] or "?") .. "(" .. p(1, 2):uint() .. ")"
  elseif t == 0x11 and len >= 4 then
    sub:add(f.seq, p(0, 2)); sub:add(f.ok, p(2, 1))
  end
  local crc_item = sub:add(f.crc, tvb(10 + len, 2))
  if crc16(tvb, 10 + len) ~= tvb(10 + len, 2):uint() then
    crc_item:add_expert_info(PI_CHECKSUM, PI_ERROR, "Bad CRC")
    info = info .. " [BAD CRC]"
  end
  pinfo.cols.info = info
  return 12 + len
end

local udp = DissectorTable.get("udp.port")
udp:add(15050, jlp)
for port = 6001, 6010 do udp:add(port, jlp) end
