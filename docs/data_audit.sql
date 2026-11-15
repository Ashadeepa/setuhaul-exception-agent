-- SetuHaul data audit: one query per finding.
-- Run against setuhaul_freight_operations.db (read-only).  Every query returns
-- the rows that prove the issue; an empty result means the issue is fixed.
-- Helper: minutes since epoch for an ISO timestamp with +05:30 offset
--   strftime('%s', substr(ts,1,19)) / 60   (all rows share the same offset)

------------------------------------------------------------------------------
-- A. TIME AND CLOCK
------------------------------------------------------------------------------

-- A01  The "snapshot" has no single clock. Check-ins stop at 09:30, most chat is
--      09:19-10:15, but one no-show is stamped 12:35 and the reefer case 16:30.
--      Decision: pick an explicit as_of time per scenario and hide later rows.
SELECT 'facility_checkins'    AS source, MIN(updated_at) AS earliest, MAX(updated_at) AS latest FROM facility_checkins
UNION ALL SELECT 'chat_messages',        MIN(message_ts), MAX(message_ts) FROM chat_messages
UNION ALL SELECT 'eta_updates',          MIN(created_at), MAX(created_at) FROM eta_updates
UNION ALL SELECT 'appointments',         MIN(updated_at), MAX(updated_at) FROM appointments
UNION ALL SELECT 'shipments',            MIN(updated_at), MAX(updated_at) FROM shipments
UNION ALL SELECT 'operational_messages', MIN(sent_at),    MAX(sent_at)    FROM operational_messages;

-- A02  Future leakage: at a 10:15 "now", these rows describe things that have not
--      happened yet. APT1018's NO_SHOW (12:35) makes SLOT-JAI-019 look AVAILABLE.
SELECT 'appointment' AS kind, appointment_id AS id, appointment_status AS state, updated_at AS stamped_at
FROM appointments WHERE updated_at > '2026-08-04T10:15:00+05:30'
UNION ALL
SELECT 'shipment', shipment_id, current_status, updated_at
FROM shipments WHERE updated_at > '2026-08-04T10:15:00+05:30';

------------------------------------------------------------------------------
-- B. ETA PROVENANCE AND CONFIDENCE
------------------------------------------------------------------------------

-- B01  latest_eta_ts differs from original_eta_ts with no eta_updates row behind it.
--      v_latest_eta then labels the value ORIGINAL_PLAN with HIGH confidence:
--      the source is unknown and the confidence is invented.
SELECT s.shipment_id, s.original_eta_ts, s.latest_eta_ts, v.eta_source, v.eta_confidence
FROM shipments s
JOIN v_latest_eta v USING (shipment_id)
WHERE s.latest_eta_ts <> s.original_eta_ts
  AND NOT EXISTS (SELECT 1 FROM eta_updates e WHERE e.shipment_id = s.shipment_id);

-- B02  A booking was placed while the driver's ETA was still unclear.
--      SHP1013: agent asked what "late by one hour" means at 09:19; no answer;
--      APT1013A was created at 09:32 on a LOW-confidence ETA.
SELECT a.appointment_id, a.shipment_id, a.booked_at, v.effective_eta_ts, v.eta_confidence,
       (SELECT MAX(m.message_ts) FROM chat_messages m JOIN chat_threads t USING (thread_id)
         WHERE t.shipment_id = a.shipment_id AND m.sender_type = 'AGENT'
           AND m.parsed_intent LIKE 'ASK_%') AS open_question_at
FROM appointments a
JOIN v_latest_eta v USING (shipment_id)
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED')
  AND a.booking_source = 'DRIVER_CHAT'
  AND v.eta_confidence = 'LOW';

-- B03  A constraint was stored before the driver confirmed its meaning.
--      EXC002 stores latest_acceptable_ts = 13:30, but the agent's own question
--      (gate-out or unload start?) was never answered.
SELECT e.exception_id, e.latest_acceptable_ts, m.message_text AS unanswered_question
FROM driver_exceptions e
JOIN chat_messages m ON m.thread_id = e.thread_id AND m.sender_type = 'AGENT'
WHERE e.latest_acceptable_ts IS NOT NULL
  AND m.parsed_intent = 'ASK_MISSING_CONSTRAINT'
  AND NOT EXISTS (SELECT 1 FROM chat_messages r
                  WHERE r.thread_id = e.thread_id AND r.sender_type = 'DRIVER'
                    AND r.message_ts > m.message_ts);

------------------------------------------------------------------------------
-- C. PHYSICALLY IMPOSSIBLE PLANS (one driver or truck in two places)
------------------------------------------------------------------------------

-- C01  Same driver or vehicle on a second shipment whose departure is earlier
--      than the first shipment can possibly be finished (gate-out if known,
--      otherwise latest ETA + unload time). Cancelled shipments excluded.
WITH legs AS (
  SELECT s.shipment_id, s.driver_id, s.vehicle_id, s.origin_city,
         COALESCE(s.actual_departure_ts, s.planned_departure_ts) AS depart_ts,
         COALESCE(c.gate_out_ts,
                  strftime('%Y-%m-%dT%H:%M:%S+05:30',
                           datetime(substr(v.effective_eta_ts,1,19), '+' || s.expected_unload_min || ' minutes'))) AS free_ts
  FROM shipments s
  JOIN v_latest_eta v USING (shipment_id)
  LEFT JOIN facility_checkins c USING (shipment_id)
  WHERE s.current_status <> 'CANCELLED'
)
SELECT a.driver_id, a.vehicle_id AS first_vehicle, a.shipment_id AS first_leg, a.free_ts AS first_leg_free_at,
       b.shipment_id AS next_leg, b.vehicle_id AS next_vehicle, b.origin_city AS next_origin, b.depart_ts AS next_departs_at
FROM legs a
JOIN legs b
  ON (a.driver_id = b.driver_id OR a.vehicle_id = b.vehicle_id)
 AND a.shipment_id <> b.shipment_id
 AND a.depart_ts <= b.depart_ts
 AND b.depart_ts < a.free_ts
ORDER BY a.driver_id;

------------------------------------------------------------------------------
-- D. BOOKINGS THAT BREAK DOCK RULES
------------------------------------------------------------------------------

-- D01  Active bookings on a dock whose weight limit is below the load.
--      (Assumption to state: max_vehicle_weight_kg compared with load weight.
--       If it means gross vehicle weight, more bookings fail.)
SELECT a.appointment_id, a.appointment_status, s.shipment_id, s.load_weight_kg,
       d.dock_code, d.max_vehicle_weight_kg
FROM appointments a
JOIN shipments s USING (shipment_id)
JOIN appointment_slots sl USING (slot_id)
JOIN docks d ON d.dock_id = sl.dock_id
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
  AND s.load_weight_kg > d.max_vehicle_weight_kg;

-- D02  Unload time longer than the slot. There is no way to book two
--      consecutive slots, so these trucks overrun into the next booking.
SELECT a.appointment_id, s.shipment_id, s.expected_unload_min,
       (strftime('%s',substr(sl.slot_end_ts,1,19)) - strftime('%s',substr(sl.slot_start_ts,1,19)))/60 AS slot_min
FROM appointments a
JOIN shipments s USING (shipment_id)
JOIN appointment_slots sl USING (slot_id)
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
  AND s.expected_unload_min >
      (strftime('%s',substr(sl.slot_end_ts,1,19)) - strftime('%s',substr(sl.slot_start_ts,1,19)))/60;

-- D03  Active bookings the truck cannot meet: latest ETA + unload time ends
--      after the slot ends. Seven bookings still hold capacity as CONFIRMED or
--      PENDING, including missed slots (APT1006, APT1015A, APT1017).
SELECT a.appointment_id, a.appointment_status, s.shipment_id, v.effective_eta_ts,
       sl.slot_start_ts, sl.slot_end_ts,
       datetime(substr(v.effective_eta_ts,1,19), '+' || s.expected_unload_min || ' minutes') AS earliest_finish
FROM appointments a
JOIN shipments s USING (shipment_id)
JOIN v_latest_eta v USING (shipment_id)
JOIN appointment_slots sl USING (slot_id)
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED')
  AND datetime(substr(v.effective_eta_ts,1,19), '+' || s.expected_unload_min || ' minutes')
      > datetime(substr(sl.slot_end_ts,1,19));

------------------------------------------------------------------------------
-- E. DOCK STATE NOT REFLECTED IN AVAILABILITY
------------------------------------------------------------------------------

-- E01  Slots that overlap a breakdown or maintenance event but are still OPEN.
--      SLOT-JAI-030 (D3 09:00-10:00) overlaps the 09:15 breakdown and still
--      carries SHP1005's CONFIRMED booking. docks.dock_status says ACTIVE.
SELECT sl.slot_id, d.dock_code, d.dock_status, sl.slot_start_ts, sl.slot_end_ts, sl.slot_status,
       ev.dock_event_id, ev.event_type, ev.event_start_ts, ev.event_end_ts,
       a.appointment_id, a.appointment_status
FROM appointment_slots sl
JOIN docks d ON d.dock_id = sl.dock_id
JOIN dock_status_events ev
  ON ev.dock_id = sl.dock_id
 AND ev.event_type IN ('BREAKDOWN','MAINTENANCE','MANUAL_BLOCK')
 AND ev.event_start_ts < sl.slot_end_ts
 AND (ev.event_end_ts IS NULL OR ev.event_end_ts > sl.slot_start_ts)
LEFT JOIN appointments a
  ON a.slot_id = sl.slot_id AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
WHERE sl.slot_status = 'OPEN';

-- E02  A truck still in the dock after its slot ended (unload overrun).
--      Later slots on that dock look free or bookable but are physically blocked.
SELECT c.shipment_id, d.dock_code, c.dock_in_ts, c.unload_start_ts, s.expected_unload_min,
       sl.slot_end_ts AS booked_slot_end,
       nxt.slot_id AS next_slot, nv.availability_status AS next_slot_shown_as, nv.appointment_id AS next_booking
FROM facility_checkins c
JOIN shipments s USING (shipment_id)
JOIN docks d ON d.dock_id = c.actual_dock_id
JOIN appointments a ON a.shipment_id = c.shipment_id AND a.appointment_status = 'IN_PROGRESS'
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
JOIN appointment_slots nxt ON nxt.dock_id = sl.dock_id AND nxt.slot_start_ts >= sl.slot_end_ts
                          AND nxt.slot_start_ts < '2026-08-04T11:00:00+05:30'
JOIN v_slot_availability nv ON nv.slot_id = nxt.slot_id
WHERE c.dock_in_ts IS NOT NULL AND c.unload_end_ts IS NULL
  AND c.updated_at > sl.slot_end_ts;

------------------------------------------------------------------------------
-- F. APPOINTMENT HISTORY AND STATUS
------------------------------------------------------------------------------

-- F01  Historical double booking: two appointments on the same slot were both
--      CONFIRMED over an overlapping period (APT1007 and APT1012A, SLOT-JAI-003,
--      from 1 Aug 11:55 until 4 Aug 09:40).
SELECT a.slot_id, a.appointment_id AS first_appt, a.confirmed_at, a.cancelled_at,
       b.appointment_id AS second_appt, b.confirmed_at, b.cancelled_at
FROM appointments a
JOIN appointments b
  ON a.slot_id = b.slot_id AND a.appointment_id < b.appointment_id
WHERE a.confirmed_at IS NOT NULL AND b.confirmed_at IS NOT NULL
  AND a.confirmed_at < COALESCE(b.cancelled_at, '9999')
  AND b.confirmed_at < COALESCE(a.cancelled_at, '9999');

-- F02  In-transit or assigned shipments with no current active appointment.
--      SHP1012 and SHP1016 lost their slot with no replacement; SHP1018 was
--      marked NO_SHOW but the shipment is still ASSIGNED.
SELECT s.shipment_id, s.current_status,
       (SELECT group_concat(a.appointment_id || ':' || a.appointment_status, ', ')
          FROM appointments a WHERE a.shipment_id = s.shipment_id) AS appointment_history
FROM shipments s
WHERE s.current_status IN ('ASSIGNED','IN_TRANSIT','AT_GATE','WAITING')
  AND NOT EXISTS (SELECT 1 FROM appointments a
                  WHERE a.shipment_id = s.shipment_id AND a.is_current = 1
                    AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS'));

-- F03  The replacement chain is never recorded. APT1013A / APT1014A are named
--      like replacements, but no earlier appointment exists for SHP1013/SHP1014
--      and replaced_appointment_id is empty on every row.
SELECT COUNT(*) AS rows_total,
       SUM(replaced_appointment_id IS NOT NULL) AS rows_with_replacement_link,
       (SELECT group_concat(shipment_id, ', ') FROM (
          SELECT shipment_id FROM appointments GROUP BY shipment_id HAVING COUNT(*) = 1
             AND MAX(appointment_id) LIKE '%A')) AS suffixed_without_history
FROM appointments;

-- F04  is_current means different things: COMPLETED is current (APT1001),
--      NO_SHOW and CANCELLED are not.
SELECT appointment_status, is_current, COUNT(*) AS n
FROM appointments
WHERE appointment_status IN ('COMPLETED','CANCELLED','NO_SHOW')
GROUP BY 1, 2;

-- F05  Pending requests have no expiry and the warehouse has not answered.
--      They block their slots indefinitely.
SELECT a.appointment_id, a.slot_id, a.booked_at, om.operational_message_id, om.delivery_status,
       (SELECT COUNT(*) FROM operational_messages r WHERE r.reply_to_message_id = om.operational_message_id) AS replies
FROM appointments a
LEFT JOIN operational_messages om ON om.appointment_id = a.appointment_id
WHERE a.appointment_status = 'PENDING_CONFIRMATION';

------------------------------------------------------------------------------
-- G. CONVERSATION AND MESSAGING
------------------------------------------------------------------------------

-- G01  Escalation recorded as done, but the message failed, and it went to an
--      address that is not a known facility contact.
SELECT om.operational_message_id, om.recipient_address, om.delivery_status,
       t.thread_id, t.thread_status, e.exception_status,
       (SELECT COUNT(*) FROM facility_contacts fc WHERE fc.email = om.recipient_address) AS known_contact
FROM operational_messages om
JOIN chat_threads t ON t.shipment_id = om.shipment_id
JOIN driver_exceptions e ON e.thread_id = t.thread_id
WHERE om.delivery_status = 'FAILED';

-- G02  Facility contacts that cannot receive email (CON005 is a vacant role).
SELECT contact_id, facility_id, contact_role, contact_name, email
FROM facility_contacts
WHERE email IS NULL OR email = '' OR contact_name LIKE 'Vacant%';

-- G03  A messaging retry opened a second thread. The two messages have different
--      external IDs, so de-duplication by message ID would miss it; dedupe_key
--      has no UNIQUE constraint. THR009 is still OPEN.
SELECT e.dedupe_key, group_concat(e.exception_id || '/' || e.thread_id || '/' || e.exception_status, ' | ') AS exceptions,
       group_concat(DISTINCT m.external_message_id) AS external_ids,
       group_concat(DISTINCT t.thread_status) AS thread_statuses
FROM driver_exceptions e
JOIN chat_threads t ON t.thread_id = e.thread_id
JOIN chat_messages m ON m.thread_id = e.thread_id AND m.sender_type = 'DRIVER'
GROUP BY e.dedupe_key
HAVING COUNT(DISTINCT e.exception_id) > 1;

-- G04  Driver messages with no agent reply at all (THR006, THR008, THR009, THR011).
SELECT t.thread_id, t.driver_id, t.shipment_id, t.thread_status, m.message_ts, m.message_text
FROM chat_threads t
JOIN chat_messages m ON m.thread_id = t.thread_id AND m.sender_type = 'DRIVER'
WHERE NOT EXISTS (SELECT 1 FROM chat_messages r WHERE r.thread_id = t.thread_id AND r.sender_type <> 'DRIVER')
ORDER BY m.message_ts;

-- G05  Ambiguous shipment (THR010) that the backend can narrow down: the driver's
--      other load (SHP1004) already gated in at 09:25, before the 09:31 "late by
--      45 minutes" message, so it most likely refers to SHP1020. Confirm, don't guess.
SELECT t.thread_id, t.opened_at, s.shipment_id, s.order_reference, s.current_status,
       c.gate_in_ts,
       CASE WHEN c.gate_in_ts IS NOT NULL AND c.gate_in_ts < t.opened_at
            THEN 'already arrived - unlikely' ELSE 'candidate' END AS reading
FROM chat_threads t
JOIN shipments s ON s.driver_id = t.driver_id AND s.current_status <> 'CANCELLED'
LEFT JOIN facility_checkins c ON c.shipment_id = s.shipment_id
WHERE t.shipment_id IS NULL;

-- G06  Arrival labels disagree with the grace rule. Both "LATE" trucks gated in
--      inside the 30-minute grace window (RULE002), so their slots were still valid.
SELECT c.shipment_id, sl.slot_start_ts, c.gate_in_ts, c.arrival_state, f.checkin_grace_min,
       (strftime('%s',substr(c.gate_in_ts,1,19)) - strftime('%s',substr(sl.slot_start_ts,1,19)))/60 AS min_after_start
FROM facility_checkins c
JOIN appointments a ON a.shipment_id = c.shipment_id AND a.is_current = 1
JOIN appointment_slots sl ON sl.slot_id = a.slot_id
JOIN facilities f ON f.facility_id = c.facility_id
WHERE c.arrival_state = 'LATE'
  AND (strftime('%s',substr(c.gate_in_ts,1,19)) - strftime('%s',substr(sl.slot_start_ts,1,19)))/60
      <= f.checkin_grace_min;

------------------------------------------------------------------------------
-- H. RULES THE SCHEMA CANNOT CHECK
------------------------------------------------------------------------------

-- H01  Slots that start at or after the last-start rule are shown AVAILABLE.
--      RULE005 says no start "after 21:00": is 21:00 itself allowed? Decide.
SELECT v.slot_id, v.dock_code, v.slot_start_ts, v.availability_status, r.rule_value AS last_new_start
FROM v_slot_availability v
JOIN facility_rules r ON r.facility_id = v.facility_id AND r.rule_type = 'LAST_NEW_START_TIME'
WHERE substr(v.slot_start_ts,12,5) >= r.rule_value
  AND v.availability_status = 'AVAILABLE';

-- H02  Rule coverage differs by facility. Gurugram has a reefer dock but no
--      reefer, heavy or last-start rule; rule_value is free text ('TRUE','21:00').
SELECT f.facility_id,
       (SELECT group_concat(DISTINCT d.dock_type) FROM docks d WHERE d.facility_id = f.facility_id) AS dock_types,
       (SELECT group_concat(r.rule_type, ', ') FROM facility_rules r WHERE r.facility_id = f.facility_id) AS rules
FROM facilities f;

-- H03  Opening hours and slot coverage do not match (Gurugram opens 07:00-21:00
--      but slots exist only 09:00-18:00).
SELECT f.facility_id, f.open_time, f.close_time,
       MIN(substr(sl.slot_start_ts,12,5)) AS first_slot, MAX(substr(sl.slot_end_ts,12,5)) AS last_slot_end
FROM facilities f JOIN appointment_slots sl USING (facility_id)
GROUP BY f.facility_id;

------------------------------------------------------------------------------
-- I. WHAT THE TWO UNIQUE INDEXES DO NOT COVER
-- Run: python3 setuhaul_constraint_probe.py setuhaul_freight_operations.db
-- (it works on a temporary copy and never changes your database).
-- Each INSERT below is ACCEPTED by the current schema. Only P1 is rejected.
------------------------------------------------------------------------------
-- P1  Two active bookings on one slot ..................... REJECTED (index works)
-- P2  Active booking on a BLOCKED slot .................... accepted
-- P3  31,000 kg heavy load on a 20,000 kg standard dock ... accepted
-- P4  Reefer load on a standard dock ...................... accepted
-- P5  Active booking for a CANCELLED shipment ............. accepted
-- P6  Second CONFIRMED booking for a shipment, is_current=0  accepted
-- P7  Jaipur shipment booked into a Gurugram slot ......... accepted
-- P8  Same truck and driver on two docks in the same hour . accepted
-- P9  Booking a slot that breaks the 21:00 last-start rule  accepted
-- Also not covered: holds (a shown or held option is not an appointment row),
-- overruns into the next slot, and pending requests that never expire.
