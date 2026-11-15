# Shows what the two partial unique indexes allow. Works on a temporary copy.
# Usage: python3 setuhaul_constraint_probe.py setuhaul_freight_operations.db
import sqlite3, sys, shutil, tempfile, os
src = sys.argv[1]
tmp = os.path.join(tempfile.mkdtemp(), "probe.db"); shutil.copy(src, tmp)
con = sqlite3.connect(tmp); con.execute("pragma foreign_keys=on")
T="2026-08-04T10:15:00+05:30"
def ins(aid, shp, slot, status, cur=1):
    con.execute("insert into appointments(appointment_id,shipment_id,slot_id,appointment_status,booking_source,is_current,booked_at,updated_at) values(?,?,?,?, 'DRIVER_CHAT',?,?,?)",(aid,shp,slot,status,cur,T,T))
probes = [
 ("P1 same slot twice (SLOT-JAI-017, two active)", lambda: (ins("X1a","SHP1012","SLOT-JAI-017","PENDING_CONFIRMATION"), ins("X1b","SHP1016","SLOT-JAI-017","PENDING_CONFIRMATION"))),
 ("P2 active booking on a BLOCKED slot (D3 11:00 breakdown)", lambda: ins("X2","SHP1012","SLOT-JAI-032","CONFIRMED")),
 ("P3 31,000 kg heavy load on 20,000 kg standard dock D1 (SLOT-JAI-006)", lambda: ins("X3","SHP1016","SLOT-JAI-006","CONFIRMED")),
 ("P4 frozen-food reefer load on a standard dock (SLOT-JAI-047)", lambda: (con.execute("update appointments set appointment_status='CANCELLED',is_current=0 where appointment_id='APT1015A'"), ins("X4","SHP1015","SLOT-JAI-047","CONFIRMED"))),
 ("P5 cancelled shipment SHP1019 gets an active booking", lambda: ins("X5","SHP1019","SLOT-JAI-048","CONFIRMED")),
 ("P6 second CONFIRMED booking for SHP1006 with is_current=0", lambda: ins("X6","SHP1006","SLOT-JAI-020","CONFIRMED",cur=0)),
 ("P7 Jaipur shipment booked into a Gurugram slot", lambda: ins("X7","SHP1012", con.execute("select slot_id from appointment_slots where facility_id='FAC-GGN-01' limit 1").fetchone()[0],"CONFIRMED")),
 ("P8 same truck VEH004 / driver DRV004 on two docks in the same hour (SHP1004 D2 09:00 + SHP1020 D4 09:00)", lambda: (con.execute("update appointments set appointment_status='CANCELLED',is_current=0 where appointment_id='APT1020'"), ins("X8","SHP1020","SLOT-JAI-044","CONFIRMED"))),
 ("P9 booking on a dock slot after the 21:00 last-start rule (SLOT-JAI-028)", lambda: ins("X9","SHP1012","SLOT-JAI-028","CONFIRMED")),
]
for name, fn in probes:
    con.execute("savepoint p")
    try:
        fn(); print(f"ACCEPTED  {name}")
    except sqlite3.IntegrityError as e:
        print(f"REJECTED  {name}  -> {e}")
    con.execute("rollback to p"); con.execute("release p")
