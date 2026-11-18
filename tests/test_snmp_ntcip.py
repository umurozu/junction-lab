"""End-to-end over real UDP sockets: client <-> agent <-> controller."""

import asyncio

import pytest

from junctionlab.controller.config import PATTERN_FLASH, four_leg
from junctionlab.controller.fsm import Controller, Mode
from junctionlab.ntcip import mib as ntcip
from junctionlab.snmp import pdu
from junctionlab.snmp.agent import MibTree, SnmpAgent
from junctionlab.snmp.ber import Oid
from junctionlab.snmp.client import SnmpClient, SnmpError, SnmpTimeout, TrapReceiver


@pytest.fixture
async def device(unused_udp_port):
    ctrl = Controller(four_leg(), pattern=1)
    for _ in range(150):  # 15 s -> stage A green
        ctrl.step(0.1)
    agent = SnmpAgent(MibTree())
    agent.mib = ntcip.build_mib(ctrl, agent.uptime, "test-junction")
    await agent.start("127.0.0.1", unused_udp_port)
    client = SnmpClient("127.0.0.1", unused_udp_port, timeout=0.5, retries=1)
    yield ctrl, agent, client
    client.close()
    agent.transport.close()


@pytest.fixture
def unused_udp_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_get_phase_status(device):
    ctrl, _, client = device
    vals = await client.get(ntcip.status_oid(ntcip.PSG_GREENS), ntcip.status_oid(ntcip.PSG_REDS),
                            ntcip.SYS_NAME)
    assert ntcip.bits(vals[ntcip.status_oid(ntcip.PSG_GREENS)]) == [1, 3]
    assert ntcip.bits(vals[ntcip.status_oid(ntcip.PSG_REDS)]) == [2, 4]
    assert vals[ntcip.SYS_NAME] == b"test-junction"


async def test_walk_phase_table(device):
    _, _, client = device
    rows = [(o, v) async for o, v in client.walk(ntcip.PHASE_TABLE)]
    min_greens = {o[-1]: v for o, v in rows if o[-2] == ntcip.PH_MIN_GREEN}
    assert min_greens == {1: 10, 2: 7, 3: 10, 4: 7}


async def test_set_pattern_to_flash(device):
    ctrl, _, client = device
    await client.set((ntcip.SYSTEM_PATTERN_CONTROL, PATTERN_FLASH))
    for _ in range(40):
        ctrl.step(0.1)
    assert ctrl.mode == Mode.FLASH
    assert await client.get_one(ntcip.COORD_PATTERN_STATUS) == PATTERN_FLASH


async def test_set_errors(device):
    _, _, client = device
    with pytest.raises(SnmpError) as e:
        await client.set((ntcip.SYSTEM_PATTERN_CONTROL, 99))
    assert e.value.status == pdu.WRONG_VALUE
    with pytest.raises(SnmpError) as e:
        await client.set((ntcip.status_oid(ntcip.PSG_GREENS), 1))
    assert e.value.status == pdu.NOT_WRITABLE
    with pytest.raises(SnmpError) as e:
        await client.set((ntcip.SYSTEM_PATTERN_CONTROL, b"x"))
    assert e.value.status == pdu.WRONG_TYPE


async def test_unknown_oid_returns_no_such_object(device):
    _, _, client = device
    from junctionlab.snmp.ber import NoSuchObject
    assert await client.get_one("1.3.6.1.4.1.1206.4.2.1.99.0") == NoSuchObject


async def test_wrong_community_is_dropped(device):
    _, agent, client = device
    client.write_community = b"wrong"
    with pytest.raises(SnmpTimeout):
        await client.set((ntcip.SYSTEM_PATTERN_CONTROL, 2))
    assert agent.stats.bad_community >= 1


async def test_trap_on_alarm(device):
    ctrl, agent, client = device
    got = asyncio.get_running_loop().create_future()
    recv = await TrapReceiver.listen(lambda addr, oid, vbs: got.done() or got.set_result((oid, vbs)),
                                     "127.0.0.1", 0)
    agent.trap_targets = [("127.0.0.1", recv.port)]
    ctrl.listeners.append(lambda kind, d: kind == "alarm" and agent.send_trap(ntcip.TRAP_ALARM))
    await client.set((ntcip.JL_FAULT_INJECT, 201))  # red lamp out on phase 1
    oid, _ = await asyncio.wait_for(got, 2)
    assert oid == ntcip.TRAP_ALARM
    assert ctrl.mode == Mode.FLASH
    recv.transport.close()


async def test_getbulk_respects_end_of_mib(device):
    _, _, client = device
    resp = await client.request(pdu.GET_BULK, [(Oid("1.3.6.1.4.1.99999.9"), None)], error_index=5)
    assert all(repr(v) == "endOfMibView" for _, v in resp.varbinds)


async def test_traffic_model_exposed(unused_udp_port):
    from junctionlab.sim.traffic import TrafficSim
    ctrl = Controller(four_leg(), pattern=1)
    sim = TrafficSim(ctrl, {1: 3600, 2: 3600}, seed=1, peds=True, ped_rate=3600)
    for _ in range(100):  # 10 s: stage A green, east (2) queues on red
        ctrl.step(0.1)
        sim.step(0.1)
    agent = SnmpAgent(MibTree())
    agent.mib = ntcip.build_mib(ctrl, agent.uptime, sim=sim)
    await agent.start("127.0.0.1", unused_udp_port)
    client = SnmpClient("127.0.0.1", unused_udp_port, timeout=0.5, retries=1)
    try:
        q2, s1, pw2 = ntcip.JL_QUEUE + (2,), ntcip.JL_SERVED + (1,), ntcip.JL_PED_WAITING + (2,)
        vals = await client.get(q2, s1, pw2)
        assert vals[q2] == sim.stats[2].queue > 0
        assert vals[s1] == sim.stats[1].served > 0
        assert vals[pw2] == sim.ped_waiting[2] > 0
    finally:
        client.close()
        agent.transport.close()
