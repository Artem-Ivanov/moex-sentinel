import { afterEach, expect, it, vi } from "vitest"

import { fetchInstruments, setInstrumentSelection, synchronizeInstruments } from "./instruments"

afterEach(() => vi.unstubAllGlobals())

it("uses broker-scoped catalog endpoints and backend filters", async () => {
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(Response.json({ items: [] })))
  vi.stubGlobal("fetch", fetchMock)

  await fetchInstruments("broker-1", "SHARE", false, true, "3000", "4000")
  await synchronizeInstruments("broker-1")
  await setInstrumentSelection("broker-1", "uid-1", true)

  expect(fetchMock).toHaveBeenNthCalledWith(
    1,
    "/api/brokers/broker-1/instruments?category=SHARE&include_inactive=false&selected_only=true&lot_price_from=3000&lot_price_to=4000&currency=RUB",
  )
  expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/brokers/broker-1/instruments/synchronize", { method: "POST" })
  expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/brokers/broker-1/instruments/uid-1/selection", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ selected: true }),
  })
})
