import { useEffect } from "react";
import { CircleMarker, MapContainer, TileLayer, Tooltip, useMap } from "react-leaflet";
import { Junction, junctionStatus, MODE_LABEL } from "../api";

const COLOR = { ok: "#22c55e", flash: "#ffb020", alarm: "#ff4d4f", offline: "#6b7280" };

function FitOnce({ junctions }: { junctions: Junction[] }) {
  const map = useMap();
  const n = junctions.length;
  useEffect(() => {
    if (n) map.fitBounds(junctions.map((j) => [j.lat, j.lon]) as [number, number][], { padding: [40, 40] });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [n]);
  return null;
}

export default function MapView({ junctions, selected, onSelect }: {
  junctions: Junction[]; selected: number | null; onSelect: (id: number) => void;
}) {
  return (
    <div className="card map">
      <MapContainer center={[39.94, 32.82]} zoom={12} style={{ height: "100%", width: "100%" }}>
        <TileLayer
          attribution='&copy; OpenStreetMap contributors'
          url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
          className="dark-tiles"
        />
        <FitOnce junctions={junctions} />
        {junctions.map((j) => {
          const st = junctionStatus(j);
          return (
            <CircleMarker key={j.id} center={[j.lat, j.lon]} radius={selected === j.id ? 13 : 9}
              pathOptions={{ color: "#0b0f17", weight: 2, fillColor: COLOR[st], fillOpacity: 0.95 }}
              className={st === "alarm" || st === "flash" ? "pulse" : ""}
              eventHandlers={{ click: () => onSelect(j.id) }}>
              <Tooltip direction="top" offset={[0, -8]}>
                <b>{j.name}</b><br />{j.online ? MODE_LABEL[j.state.mode_name ?? "?"] : "Offline"}
              </Tooltip>
            </CircleMarker>
          );
        })}
      </MapContainer>
    </div>
  );
}
