import { createRouter, createWebHistory } from "vue-router"

import BrokerAccountsView from "./views/BrokerAccountsView.vue"
import BrokersView from "./views/BrokersView.vue"
import PositionDetailsView from "./views/PositionDetailsView.vue"
import PositionsListView from "./views/PositionsListView.vue"
import InstrumentItemView from "./views/InstrumentItemView.vue"
import InstrumentsListView from "./views/InstrumentsListView.vue"

export const router = createRouter({
  history: createWebHistory(),
  routes: [
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
