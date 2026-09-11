import { cleanup, fireEvent, render, screen } from "@testing-library/vue"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { BrokerAdapterDefinition, BrokerDraft } from "../api/brokers"
import BrokerForm from "./BrokerForm.vue"

const adapters: BrokerAdapterDefinition[] = [
  {
    adapter_code: "TINVEST_SANDBOX",
    provider_code: "TINVEST",
    environment_code: "SANDBOX",
    fields: [
      { name: "token", required: true, default_value: null },
      { name: "fqdn", required: true, default_value: "sandbox.example:443" },
    ],
  },
]

const editDraft: BrokerDraft = {
  display_name: "Sandbox",
  provider_code: "TINVEST",
  environment_code: "SANDBOX",
  adapter_code: "TINVEST_SANDBOX",
  enabled: true,
  fields: [
    { name: "token", value: "test-connection-value" },
    { name: "fqdn", value: "sandbox.example:443" },
  ],
  is_test: true,
  account_id: "account-1",
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("BrokerForm", () => {
  it("locks broker identity while emitting copied mutable edit values", async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)
    const { emitted } = render(BrokerForm, {
      props: {
        mode: "edit",
        draft: editDraft,
        adapters,
        fieldErrors: {},
      },
    })

    expect(screen.getByRole("heading", { name: "Редактирование интеграции" })).toBeTruthy()
    expect((screen.getByLabelText("Адаптер") as HTMLSelectElement).disabled).toBe(true)
    expect((screen.getByLabelText("Тестовое подключение") as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByLabelText("Название") as HTMLInputElement).value).toBe("Sandbox")

    await fireEvent.update(screen.getByLabelText("Название"), "Sandbox updated")

    expect(emitted()["update:draft"]?.[0]).toEqual([{ ...editDraft, display_name: "Sandbox updated" }])
    expect(emitted()["clear-field"]?.[0]).toEqual(["display_name"])
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it("emits cloned connection settings and backend field paths", async () => {
    const { emitted } = render(BrokerForm, {
      props: {
        mode: "edit",
        draft: editDraft,
        adapters,
        fieldErrors: { "fields.token": "Заполните поле подключения." },
      },
    })

    const token = screen.getByLabelText("token")
    expect(token.getAttribute("aria-invalid")).toBe("true")
    expect(screen.getByText("Заполните поле подключения.")).toBeTruthy()

    await fireEvent.update(token, "updated-connection-value")

    expect(emitted()["update:draft"]?.[0]).toEqual([
      {
        ...editDraft,
        fields: [
          { name: "token", value: "updated-connection-value" },
          { name: "fqdn", value: "sandbox.example:443" },
        ],
      },
    ])
    expect(emitted()["clear-field"]?.[0]).toEqual(["fields.token"])
    expect(editDraft.fields[0].value).toBe("test-connection-value")
  })

  it("keeps adapter selection available when creating a broker", async () => {
    const draft: BrokerDraft = {
      display_name: "",
      provider_code: "",
      environment_code: "",
      adapter_code: "",
      enabled: true,
      fields: [],
      is_test: true,
      account_id: null,
    }
    const { emitted } = render(BrokerForm, {
      props: { mode: "create", draft, adapters, fieldErrors: {} },
    })

    expect(screen.getByRole("heading", { name: "Новая интеграция" })).toBeTruthy()
    expect((screen.getByLabelText("Адаптер") as HTMLSelectElement).disabled).toBe(false)
    expect((screen.getByLabelText("Тестовое подключение") as HTMLInputElement).disabled).toBe(false)

    await fireEvent.update(screen.getByLabelText("Адаптер"), "TINVEST_SANDBOX")

    expect(emitted()["select-adapter"]).toEqual([["TINVEST_SANDBOX"]])
    expect(emitted()["clear-field"]?.[0]).toEqual(["adapter_code"])
  })

  it("emits submit and cancel without making requests", async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)
    const { emitted } = render(BrokerForm, {
      props: { mode: "edit", draft: editDraft, adapters, fieldErrors: {} },
    })

    await fireEvent.click(screen.getByRole("button", { name: "Сохранить" }))
    await fireEvent.click(screen.getByRole("button", { name: "Отмена" }))

    expect(emitted().submit).toHaveLength(1)
    expect(emitted().cancel).toHaveLength(1)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
