import { createRouter, createWebHistory, type Router } from "vue-router"

import { currentSession, restoreSession, safeInternalRedirect, sessionChecked, setUnauthorizedHandler } from "./auth"
import BrokerAccountsView from "./views/BrokerAccountsView.vue"
import BrokersView from "./views/BrokersView.vue"
import LoginView from "./views/LoginView.vue"
import DiagnosticsView from "./views/DiagnosticsView.vue"
import PositionDetailsView from "./views/PositionDetailsView.vue"
import PositionsListView from "./views/PositionsListView.vue"
import InstrumentItemView from "./views/InstrumentItemView.vue"
import InstrumentsListView from "./views/InstrumentsListView.vue"

export function installAuthGuard(target: Router): void {
  target.beforeEach(async (to) => {
    if (!sessionChecked.value) await restoreSession()
    if (to.name === "login") {
      if (!currentSession.value) return true
      return safeInternalRedirect(to.query.redirect) ?? { name: "positions" }
    }
    if (!currentSession.value) return { name: "login", query: { redirect: to.fullPath } }
    return true
  })
}

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: LoginView },
    { path: "/diagnostics", name: "diagnostics", component: DiagnosticsView },
    { path: "/", redirect: { name: "positions" } },
    { path: "/brokers", name: "brokers", component: BrokersView },
    { path: "/brokers/:brokerId/accounts", name: "broker-accounts", component: BrokerAccountsView },
    { path: "/instruments", name: "instruments", component: InstrumentsListView },
    { path: "/instruments/:brokerId/:instrumentId", name: "instrument-details", component: InstrumentItemView },
    { path: "/positions", name: "positions", component: PositionsListView },
    { path: "/positions/:id", name: "position-details", component: PositionDetailsView },
    { path: "/operations", redirect: { name: "positions" } },
  ],
})

installAuthGuard(router)
setUnauthorizedHandler(() => {
  const route = router.currentRoute.value
  if (route.name !== "login") {
    void router.replace({ name: "login", query: { redirect: route.fullPath } })
  }
})
