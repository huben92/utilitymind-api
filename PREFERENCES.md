# Project preferences — UtilityMind AI

Updated: 2026-10-17. This file records the product direction for the Wheelworks factory prototype. The user's latest project brief takes precedence over suggestions in the [shared planning conversation](https://chatgpt.com/s/t_6ad0da4c969c8191b576ee6fbc15a32b) and over the prototype's current feature set.

## Purpose and users

- Detect utility overuse, undersupply, and consumption that does not match production activity. Show enough evidence for people to investigate and act.
- Utility managers monitor live conditions, anomalies, likely explanations, and recommendations. Executives see concise trends, production impact, and decision-ready KPIs.
- Treat the application as a decision-support system. It informs and recommends; it does not automatically change utility supply or machine setpoints.

## Utility scope and terminology

- Cover **electricity, clean water, compressed air, natural gas, diesel, chilled water, and steam/hot water (steam/air panas)**. Preserve the distinction between steam and hot water until the metering medium and units are specified.
- Monitor **chillers** as systems that produce chilled water and **boilers** as systems that produce steam/hot water. Their electricity, fuel, flow, temperature, pressure, output, capacity, and status may be relevant to both system health and utility accounting. Avoid double counting their inputs and outputs.
- Use **metering** or **metering point** in the user interface. The existing database and MQTT fields `sensors` and `sensor_id` may remain for compatibility.
- Do not restore **waste** or **capacity/occupancy** as measured utility categories. Equipment capacity and operating limits are still required as system metadata and analytical constraints.
- Keep rates and accumulated quantities distinct: for example, `kW` is live electric power and `kWh` is energy over time; `m³/h` is flow and `m³` is volume. Show the measurement period and unit for every KPI.

## Required context and data

- Collect timestamped metering for each utility and producing system, with equipment, location, production line, quality/status, and unit metadata.
- Record equipment capacity, safe operating ranges, minimum supply requirements, availability, and relevant process variables such as pressure, temperature, flow, and power.
- Record actual production output, targets, operating schedules, line status, and employee shifts so utility use can be evaluated against demand and reported per shift.
- Calculate baselines for comparable production and operating conditions. A fixed threshold alone cannot establish an anomaly when output, schedules, or necessary standby loads change.
- Simulated data should reflect equipment behavior and production demand, including realistic low production, peaks, idle operation, supply shortages, and efficiency loss. Label simulated data and results clearly.

## AI and analysis behavior

- Detect excess use, insufficient supply, and use that conflicts with production activity. Show the actual value, expected range, difference, affected equipment, time window, production context, and confidence or uncertainty.
- Explain **possible** causes from supporting data; do not present a leak, fault, or operator action as certain without evidence. Let operators verify, dismiss, or annotate anomalies.
- Forecast utility demand from production schedules and current conditions where data supports it. Evaluate what-if operating scenarios within equipment and production limits.
- Recommendations should state the expected effect, assumptions, constraints, and evidence. Any optimization remains advisory and must preserve production needs and safe operating ranges.
- Generate manager reports and executive summaries, including per-shift results. A future AI copilot may explain calculations and evidence in natural language; it must not invent measurements or savings.
- The AI chat copilot sends the approved PostgreSQL and ClickHouse schema context on each request, requests bounded read-only SQL through the app, and explains the returned evidence. Keep it limited to this factory's utility project. Render structured charts and tables from returned rows; never execute AI-generated HTML. Its provider URL, model, and API key are configured in App Settings, and the key stays server-side.

## Dashboard and reporting preferences

- Preserve the factory → location → production line → machine → metering hierarchy, the production-flow map, live status, category detail, machine trend, and machine share of utility use.
- Add views for production-adjusted baselines, shortages, anomalies, chiller/boiler performance, utility availability, recommendations, and shift reports as these capabilities are implemented.
- Show the latest reading and staleness clearly. An off or stale machine must not be presented as a live zero reading.
- Primary KPIs: utilization percentage for each utility, verified anomaly count, and alignment of utility use with production output, each viewable per shift. Also track useful supporting measures such as specific consumption per unit, supply compliance, production target achievement, and cost when reliable tariff data exists.
- Distinguish **measured savings**, **simulated savings**, and **estimated opportunities**. Compare like-for-like production conditions and report assumptions; never claim energy savings if the apparent reduction comes from reduced production or unmet supply.

## Implementation state and priorities

- Current local prototype: simulator → local MQTT broker → dashboard subscriber → ClickHouse telemetry; PostgreSQL stores master data. The dashboard demonstration now models seven categories: electricity, clean water, compressed air, natural gas, diesel, chilled water, and steam. Steam and hot water remain distinct media; hot water has no demo meter. Daily and monthly metering reports can be viewed and downloaded as PDFs, with coverage and missing-input caveats. AI chat uses a user-configured OpenAI-compatible endpoint and remains unavailable until that endpoint and model are set. The dashboard interface is in English. Work Plans have been retired from the dashboard; historical records remain in PostgreSQL but do not drive current overview notices.
- Operator-configured metering baselines and operational maximums remain in PostgreSQL master data. A separate three-month synthetic history supplies weekday/15-minute expected rates and empirical ranges; it does not overwrite configured values or establish production-adjusted targets. Simulator sliders control current MQTT readings independently and cannot change master thresholds. The slider can exceed the operational maximum for testing. Existing baselines that were previously changed by the old slider must be reviewed and corrected manually where necessary.
- Configured-baseline, operational-maximum, and simulated historical high/low deviations create separate advisory metering notifications. Keep one open episode per metering point and threshold type, retain its peak and reading count, close it only after a reading returns to or below that threshold, and retain resolved history. Show the last reading time so missing telemetry is not mistaken for a currently measured overuse. These notices are not verified anomalies or leak diagnoses. Overview, category, and machine trend charts compare live rates with the time-matched simulated historical expected trend and a separate configured operational maximum. Supply-output meters are excluded from factory consumption totals to avoid double counting.
- A sustained-maximum notice requires at least one minute of uninterrupted above-maximum readings. Machine-off questions require every enabled meter on that machine to read zero continuously for one minute with fresh telemetry; a partial-zero condition instead prompts a metering check. A disabled machine reporting positive metering prompts a restart question. Machine master status changes only when a user confirms off or on. Threshold notices can be resolved with an explanation or ignored; the action suppresses duplicates until the condition clears. A missing stream never counts as zero.
- Seven utility categories and illustrative chiller/boiler support machines are now modeled with synthetic data. Actual machine inventories, rated capacities, production and shift data, anomaly verification, and AI recommendations remain **product requirements**, not claims that the prototype already implements them.
- The shared conversation proposed an earlier three-system MVP focused on compressed air, chiller, and pumping, plus a roadmap for baseline detection, demand forecasting, constrained optimization, and an AI copilot. Use that roadmap as planning context; the updated seven-utility scope and advisory-only safety rule govern future work. Pumping can be modeled as equipment or a distribution system without making it an eighth measured utility.
- For demos and tests, prefer a working pipeline and traceable calculations over static mockups. Keep historical, simulated, and live observations distinguishable.

## Case-study lessons supplied by the user

- **Gates Canada:** submetering plus five-minute, production-adjusted targets made area-level gas and electricity use visible. The case reports gas down 8.2%, electricity down 12.3%, and US$240,000 first-year savings after data-driven operational decisions and repairs. [Union Gas–Gates case study](https://rodanenergy.com/wp-content/uploads/2022/04/Case-Study-UnionGas-Gates.pdf).
- **Saint-Gobain:** submetering exposed equipment running longer than the process needed and loads operating without product. The user-provided case reports a 14% reduction in electricity intensity per unit and about 15% annual electricity-cost opportunity from four projects; do not attribute the entire 14% change to monitoring alone.
- **Carlsberg Canada:** process-level water flow revealed water use while a line was stopped. The user-provided case reports a facility-wide annual reduction above 100,000 m³; do not attribute all of it to one scheduling change.
- **Johnson Controls:** combined flow, pressure, and compressor-current monitoring supported finding nonproductive compressed-air use and checking repairs. The user-provided case reports 39% less compressed-air electricity per unit of production and 37% lower monthly electricity cost from the combined monitoring and operational program.

Use these cases to guide metering granularity, production-aware comparisons, and evidence presentation. Verify original sources before using their numbers as external proof or promising similar outcomes for this factory.
