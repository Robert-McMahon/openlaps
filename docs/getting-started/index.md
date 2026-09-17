# Overview

openlaps is split between a vehicle computer and a pit computer.

## Before you deploy

You need:

- a Linux vehicle computer with SocketCAN and a supported GNSS path;
- a Linux pit computer with Docker Compose;
- IP reachability from the pit to the vehicle on the radio network;
- Python 3.13 and [uv](https://docs.astral.sh/uv/) for development;
- per-deployment NATS TLS material and passwords.

The pit initiates the TLS leafnode connection to the vehicle on port `7422`.
That means the vehicle must be reachable from the pit network. NAT or a default
WSL2 NAT boundary needs explicit routing or port forwarding.

## Read in this order

1. [Architecture](../ARCHITECTURE.md) for system boundaries and data flow.
2. [Project status](../status.md) for implemented and outstanding work.
3. [Vehicle installation](../operations/vehicle.md) and
   [pit installation](../operations/pit.md) for deployment.
4. [Verify the stack](../operations/verification.md) before a session.

## Configuration boundaries

- A **profile** describes the car: buses, sensors, DBCs, channels and alarms.
- A **target** describes the vehicle computer: device names, CAN link settings,
  temperatures and video encoder.
- Pit configuration selects live channels, dashboards, notifications and
  external timing feeds.

Keeping these separate lets a car move to another computer without renaming
channels or editing its DBCs.
