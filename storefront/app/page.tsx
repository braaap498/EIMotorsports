
import VehicleSelector from "@/components/VehicleSelector";

export default function Home() {
  return (
    <main className="mx-auto max-w-4xl p-6">
      <h1 className="mb-4 text-2xl font-bold">Find parts for your vehicle</h1>
      <VehicleSelector />
    </main>
  );
}