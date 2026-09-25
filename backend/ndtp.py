"""Bounded, CRC-validated NDTP 6.2 decoder for the supplied emulator.

Unknown cell layouts stop optional decoding rather than guessing byte offsets.
The leading Nav00 is retained; sensor data is not interpreted as door status.
"""
import asyncio, struct
from datetime import datetime, timezone

SIZES={0:26,2:26,8:6,10:37,15:50,16:8}

def crc16(data):
    """Return the CRC-16/Modbus checksum for a byte sequence."""
    crc=0xffff
    for byte in data:
        crc^=byte
        for _ in range(8): crc=(crc>>1)^0xa001 if crc&1 else crc>>1
    return crc

def decode(frame):
    """Validate and decode one complete NDTP frame into normalized telemetry events."""
    if len(frame)<25: raise ValueError('short frame')
    sig,size,flags,stored,kind,unit,request=struct.unpack_from('<HHHHBIH',frame)
    if sig!=0x7e7e or len(frame)!=15+size or size<10 or kind!=2 or flags!=0: raise ValueError('invalid NPL')
    crc=crc16(frame[15:]);swapped=((crc&255)<<8)|(crc>>8)
    if stored!=swapped: raise ValueError('CRC mismatch')
    service,typ,nflags,req=struct.unpack_from('<HHHI',frame,15)
    if service==0 and typ==100:
        if size!=28: raise ValueError('invalid handshake')
        return []
    if service!=1 or typ!=101: raise ValueError('unsupported NPH')
    offset=25;events=[]
    while offset<len(frame):
        if len(frame)-offset<2: raise ValueError('truncated cell')
        cell,number=struct.unpack_from('<BB',frame,offset);offset+=2
        if cell not in SIZES: break
        size=SIZES[cell]
        if offset+size>len(frame): raise ValueError('truncated cell payload')
        if cell==0:
            ts,lon,lat,bits,voltage,speed,speedmax,course,track,alt,nsat,pdop=struct.unpack_from('<IIIBBHHHHHBB',frame,offset)
            events.append(dict(unit_id=unit,event_time=datetime.fromtimestamp(ts,tz=timezone.utc).isoformat(),lon=lon/1e7*(1 if bits&64 else -1),lat=lat/1e7*(1 if bits&32 else -1),speed=speed,location_valid=bool(bits&128)))
        offset+=size
    return events

async def handle(reader,writer,callback,counters):
    """Read NDTP frames from one TCP connection and forward valid events to callback."""
    try:
        while True:
            header=await asyncio.wait_for(reader.readexactly(15),timeout=120)
            sig,size=struct.unpack_from('<HH',header)
            if sig!=0x7e7e or not 10<=size<=65535: raise ValueError('invalid frame length')
            body=await asyncio.wait_for(reader.readexactly(size),timeout=10)
            for event in decode(header+body): await callback(event)
            counters['ndtp_packets']+=1
    except (asyncio.IncompleteReadError,ConnectionError,asyncio.TimeoutError):
        counters['ndtp_disconnects']+=1
    except (ValueError,struct.error):
        counters['ndtp_errors']+=1
    finally:
        writer.close()
        try: await writer.wait_closed()
        except ConnectionError: pass
