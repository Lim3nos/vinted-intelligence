# Retool Dashboard — Guide de configuration complet

## 1. Déploiement Railway (prérequis)

### A. Créer le dépôt GitHub
```bash
# Sur GitHub, créer un repo "vinted-intelligence"
# Puis :
git remote add origin https://github.com/TON_USER/vinted-intelligence.git
git push -u origin master
```

### B. Déployer sur Railway
1. Aller sur https://railway.app → New Project → Deploy from GitHub
2. Choisir le repo `vinted-intelligence`
3. Railway détecte automatiquement `railway.toml`
4. Dans **Variables** d'environnement, ajouter :
   - `DATABASE_URL` → ta chaîne Supabase complète
   - `GEMINI_API_KEY` → ta clé Google AI Studio
   - `VINTED_BASE_URL` → `https://www.vinted.fr`
   - `SECRET_KEY` → chaîne aléatoire 32 chars
   - `ENVIRONMENT` → `production`
   - `LOG_LEVEL` → `INFO`
5. Déployer → noter l'URL (ex: `https://vinted-api.up.railway.app`)

---

## 2. Ressources Retool

### Resource 1 — PostgreSQL (Supabase)
- **Nom** : `vinted_db`
- **Host** : `db.kkmvatsdfhvjdyyvqygp.supabase.co`
- **Port** : `5432`
- **Database** : `postgres`
- **User** : `postgres`
- **Password** : ton mot de passe Supabase
- **SSL** : enabled

### Resource 2 — REST API
- **Nom** : `vinted_api`
- **Base URL** : `https://TON_APP.up.railway.app`
- **Headers** : `Content-Type: application/json`

---

## 3. Pages et composants

---

### PAGE 1 — Dashboard (page d'accueil)

**Objectif** : Vue d'ensemble de tous les modèles surveillés avec score de signal.

#### Query `q_models` (PostgreSQL)
```sql
SELECT
  pm.id,
  pm.model_name,
  pm.signal_score,
  pm.priority,
  s.name AS search_name,
  pm.created_at,
  COUNT(DISTINCT l.id) FILTER (WHERE l.is_sold = false AND l.is_active = true) AS active_listings,
  COUNT(DISTINCT l.id) FILTER (WHERE l.is_sold = true) AS sold_count,
  ROUND(AVG(l.price) FILTER (WHERE l.is_sold = true)::numeric, 0) AS avg_sold_price,
  MAX(l.first_seen_at) AS last_listing_at
FROM product_models pm
JOIN searches s ON s.id = pm.search_id
LEFT JOIN listings l ON l.product_model_id = pm.id
WHERE pm.is_active = true
GROUP BY pm.id, pm.model_name, pm.signal_score, pm.priority, s.name, pm.created_at
ORDER BY pm.signal_score DESC NULLS LAST
LIMIT 50
OFFSET {{ (table_models.pageIndex ?? 0) * 50 }}
```

#### Query `q_models_count` (PostgreSQL)
```sql
SELECT COUNT(*) AS total FROM product_models WHERE is_active = true
```

#### Query `q_health` (REST API)
- **Method** : GET
- **Endpoint** : `/api/health`
- **Run on load** : true

#### Composants
| Composant | Type | Config |
|-----------|------|--------|
| `table_models` | Table | Data: `{{ q_models.data }}` — Colonnes: model_name, search_name, signal_score (badge couleur), active_listings, sold_count, avg_sold_price, priority |
| `badge_status` | Badge | Text: `{{ q_health.data.status }}` — Color: `{{ q_health.data.status === 'ok' ? 'green' : 'red' }}` |
| `text_last_snap` | Text | Value: `Dernier snapshot : {{ q_health.data.hours_since_last_snapshot }}h` |
| `badge_gemini` | Badge | Text: `{{ q_health.data.gemini_circuit_open ? 'Gemini SUSPENDU' : 'Gemini OK' }}` — Color: red/green |
| `btn_recalculate` | Button | Label: Recalculer les scores — Action: Trigger `q_recalc` |
| `stat_active` | Statistic | Value: `{{ q_health.data.active_searches }}` — Label: Recherches actives |

#### Query `q_recalc` (REST API)
- **Method** : POST
- **Endpoint** : `/api/admin/recalculate-scores`
- **Success alert** : "Scores recalculés"

#### Badge couleur signal_score (colonne table)
```js
// Valeur de la cellule
const s = self.value;
return s >= 70 ? 'green' : s >= 40 ? 'yellow' : 'red';
```

---

### PAGE 2 — Vue Modèle

**Objectif** : Détail complet d'un modèle : annonces, heatmap, historique de prix, score.

#### Variable de page : `model_id` (passée depuis Dashboard via `navigateTo`)

#### Query `q_model_detail` (PostgreSQL)
```sql
SELECT
  pm.*,
  s.name AS search_name,
  COUNT(DISTINCT l.id) FILTER (WHERE l.is_sold = false AND l.is_active = true) AS active_count,
  COUNT(DISTINCT l.id) FILTER (WHERE l.is_sold = true) AS sold_count,
  ROUND(MIN(l.price) FILTER (WHERE l.is_sold = false AND l.is_active = true)::numeric, 0) AS min_price,
  ROUND(MAX(l.price) FILTER (WHERE l.is_sold = false AND l.is_active = true)::numeric, 0) AS max_price,
  ROUND(AVG(l.price) FILTER (WHERE l.is_sold = true)::numeric, 0) AS avg_sold_price
FROM product_models pm
JOIN searches s ON s.id = pm.search_id
LEFT JOIN listings l ON l.product_model_id = pm.id
WHERE pm.id = {{ urlparams.model_id }}
GROUP BY pm.id, s.name
```

#### Query `q_listings` (PostgreSQL)
```sql
SELECT
  l.id,
  l.title,
  ROUND(l.price::numeric, 0) AS price,
  l.seller_id,
  l.item_condition,
  l.favourite_count,
  l.is_sold,
  l.is_active,
  l.first_seen_at,
  l.last_seen_at,
  ROUND(l.time_to_disappear_hours::numeric, 1) AS hours_online,
  l.vinted_url
FROM listings l
WHERE l.product_model_id = {{ urlparams.model_id }}
  AND ({{ select_listing_filter.value }} = 'all'
       OR ({{ select_listing_filter.value }} = 'active' AND l.is_sold = false AND l.is_active = true)
       OR ({{ select_listing_filter.value }} = 'sold' AND l.is_sold = true))
ORDER BY l.first_seen_at DESC
LIMIT 50
OFFSET {{ (table_listings.pageIndex ?? 0) * 50 }}
```

#### Query `q_heatmap` (REST API)
- **Method** : GET
- **Endpoint** : `/api/models/{{ urlparams.model_id }}/heatmap`

#### Query `q_price_history` (PostgreSQL)
```sql
SELECT
  ph.recorded_at,
  ROUND(ph.old_price::numeric, 0) AS old_price,
  ROUND(ph.new_price::numeric, 0) AS new_price,
  l.title,
  l.item_condition
FROM price_history ph
JOIN listings l ON l.id = ph.listing_id
WHERE l.product_model_id = {{ urlparams.model_id }}
ORDER BY ph.recorded_at DESC
LIMIT 50
```

#### Query `q_score_history` (PostgreSQL)
```sql
SELECT
  ss.created_at,
  ss.total_results,
  ss.new_listings,
  ss.estimated_sold
FROM search_snapshots ss
JOIN searches s ON s.id = ss.search_id
JOIN product_models pm ON pm.search_id = ss.search_id
WHERE pm.id = {{ urlparams.model_id }}
ORDER BY ss.created_at DESC
LIMIT 30
```

#### Composants
| Composant | Type | Config |
|-----------|------|--------|
| `text_model_name` | Text | `{{ q_model_detail.data[0]?.model_name }}` — style H1 |
| `badge_score` | Badge | `{{ q_model_detail.data[0]?.signal_score ?? 'N/A' }}` |
| `stat_active` | Statistic | `{{ q_model_detail.data[0]?.active_count }}` — Actives |
| `stat_sold` | Statistic | `{{ q_model_detail.data[0]?.sold_count }}` — Vendues |
| `stat_avg_sold` | Statistic | `{{ q_model_detail.data[0]?.avg_sold_price }}€` — Prix moy. vente |
| `select_listing_filter` | Select | Options: all/active/sold — Default: all |
| `table_listings` | Table | Data: `{{ q_listings.data }}` — Colonnes: title, price, item_condition, favourite_count, is_sold (badge), hours_online, lien vinted_url |
| `chart_heatmap` | Heatmap Chart | `{{ q_heatmap.data }}` — x: hour, y: day_of_week, value: avg_velocity |
| `chart_price_history` | Line Chart | `{{ q_price_history.data }}` — x: recorded_at, y: new_price |
| `btn_suggest_price` | Button | Navigue vers Page Prix avec model_id |

**Lien Vinted dans la table :**
```js
// Colonne vinted_url → type Link
// Label: "Voir sur Vinted"
// URL: {{ currentRow.vinted_url }}
```

---

### PAGE 3 — Exploration

**Objectif** : Lancer une exploration IA sur une marque ou un mot-clé, voir les clusters.

#### Composants formulaire
| Composant | Type | Config |
|-----------|------|--------|
| `input_query` | Input | Placeholder: "Lemaire, Toteme, Sézane..." |
| `select_search_type` | Select | Options: brand / model / keyword |
| `input_price_min` | Number Input | Default: 30 |
| `input_price_max` | Number Input | Default: 300 |
| `select_filter_level` | Select | Options: 1 / 2 / 3 / 4 / 5 — Default: 3 |
| `btn_start` | Button | Label: Lancer l'exploration |
| `text_status` | Text | Affiche le statut du job en cours |
| `container_results` | Container | Visible: `{{ state_job_done.value }}` |

#### Query `q_start_job` (REST API)
- **Method** : POST
- **Endpoint** : `/api/exploration/start`
- **Body** :
```json
{
  "search_type": "{{ select_search_type.value }}",
  "query": "{{ input_query.value }}",
  "price_min": {{ input_price_min.value }},
  "price_max": {{ input_price_max.value }},
  "filter_level": {{ select_filter_level.value }}
}
```
- **On success** : `setState(state_job_id, data.job_id); setState(state_job_done, false); q_poll_job.trigger()`

#### State variables
| Nom | Type | Default |
|-----|------|---------|
| `state_job_id` | string | "" |
| `state_job_done` | boolean | false |
| `state_job_result` | object | {} |

#### Query `q_poll_job` (REST API) — **Polling**
- **Method** : GET
- **Endpoint** : `/api/jobs/{{ state_job_id.value }}/status`
- **Run on load** : false
- **Enable polling** : true — **interval** : 3000ms
- **On success** :
```js
// Dans "Success event handler" → Run script
const d = data;
if (d.status === 'completed' || d.status === 'failed') {
  q_poll_job.stopPolling();
  setState(state_job_done, true);
  setState(state_job_result, d);
}
```

#### Barre de progression
```js
// Text du composant text_status
const s = q_poll_job.data?.status;
const sec = q_poll_job.data?.elapsed_seconds;
if (!state_job_id.value) return "Remplis le formulaire et lance l'exploration.";
if (s === 'pending') return "⏳ En attente...";
if (s === 'running') return `⚙️ Scraping en cours... (${sec}s)`;
if (s === 'completed') return `✅ Terminé en ${sec}s`;
if (s === 'failed') return `❌ Erreur : ${q_poll_job.data?.error_message}`;
return "...";
```

#### Query `q_validate_cluster` (REST API)
- **Method** : POST
- **Endpoint** : `/api/exploration/validate-cluster`
- **Body** :
```json
{
  "search_id": {{ select_search_for_cluster.value }},
  "model_name": "{{ input_cluster_name.value }}",
  "suggested_keywords": {{ JSON.stringify(input_keywords.value.split(',').map(k => k.trim())) }},
  "nb_listings": {{ selected_cluster.nb_listings }},
  "median_price": {{ selected_cluster.median_price }}
}
```

#### Table des clusters
```sql
-- Pas de table SQL — data vient du state_job_result
-- Data: {{ (state_job_result.value?.result?.clusters ?? []) }}
```

| Composant | Config |
|-----------|--------|
| `table_clusters` | Data: `{{ state_job_result.value?.result?.clusters ?? [] }}` — Colonnes: model_name, nb_listings, median_price, avg_favourites |
| `text_filter_stats` | `Scrappé: {{ state_job_result.value?.result?.total_scraped }} → L3: {{ state_job_result.value?.result?.filtered?.level_3 }} → L5: {{ state_job_result.value?.result?.filtered?.level_5 }}` |
| `btn_validate` | Valider le cluster sélectionné → trigger `q_validate_cluster` |
| `select_search_for_cluster` | Select alimenté par `q_searches.data` |
| `input_cluster_name` | Pré-rempli avec `{{ table_clusters.selectedRow?.model_name }}` |

---

### PAGE 4 — Suggesteur de Prix

**Objectif** : Obtenir un prix recommandé pour un article avant de le mettre en vente.

#### Query `q_models_list` (PostgreSQL)
```sql
SELECT id, model_name FROM product_models WHERE is_active = true ORDER BY model_name
```

#### Query `q_suggest` (REST API)
- **Method** : POST
- **Endpoint** : `/api/price/suggest`
- **Body** :
```json
{
  "product_model_id": {{ select_model.value }},
  "my_item_status": "{{ select_status.value }}"
}
```
- **Run on load** : false

#### Composants
| Composant | Type | Config |
|-----------|------|--------|
| `select_model` | Select | Data: `{{ q_models_list.data }}` — labelKey: model_name, valueKey: id |
| `select_status` | Select | Options: Neuf avec étiquette / Neuf sans étiquette / Très bon état / Bon état / Satisfaisant |
| `btn_suggest` | Button | Label: Calculer le prix → trigger `q_suggest` |
| `stat_suggested` | Statistic | `{{ q_suggest.data?.suggested_price ?? '-' }}€` — Prix recommandé |
| `stat_range` | Statistic | `{{ q_suggest.data?.price_range ? q_suggest.data.price_range[0] + '–' + q_suggest.data.price_range[1] + '€' : '-' }}` — Fourchette |
| `badge_confidence` | Badge | `{{ q_suggest.data?.confidence ?? '-' }}` — Color: high=green, medium=yellow, low=red |
| `stat_sample` | Statistic | `{{ q_suggest.data?.sample_size ?? 0 }}` — Ventes de référence |
| `text_method` | Text | `{{ q_suggest.data?.method }}` — Méthode utilisée |

#### Historique des prix du modèle sélectionné
```sql
-- Query q_price_hist_model
SELECT
  ph.recorded_at,
  ROUND(ph.new_price::numeric, 0) AS price,
  l.item_condition AS status,
  l.title
FROM price_history ph
JOIN listings l ON l.id = ph.listing_id
WHERE l.product_model_id = {{ select_model.value }}
  AND l.is_sold = true
ORDER BY ph.recorded_at DESC
LIMIT 30
```

---

### PAGE 5 — Journal de Revente

**Objectif** : Suivre ses achats et ventes, calculer les profits.

#### Query `q_journal` (PostgreSQL)
```sql
SELECT
  je.id,
  je.item_title,
  pm.model_name,
  je.purchase_price,
  je.sale_price,
  je.purchase_date,
  je.sale_date,
  je.item_status,
  je.notes,
  CASE WHEN je.sale_price IS NOT NULL
       THEN ROUND((je.sale_price - je.purchase_price)::numeric, 0)
       ELSE NULL
  END AS profit,
  CASE WHEN je.sale_price IS NOT NULL
       THEN ROUND(((je.sale_price - je.purchase_price) / NULLIF(je.purchase_price, 0) * 100)::numeric, 1)
       ELSE NULL
  END AS profit_pct
FROM journal_entries je
LEFT JOIN product_models pm ON pm.id = je.product_model_id
ORDER BY je.purchase_date DESC
LIMIT 50
OFFSET {{ (table_journal.pageIndex ?? 0) * 50 }}
```

#### Query `q_journal_stats` (PostgreSQL)
```sql
SELECT
  COUNT(*) AS total_items,
  COUNT(*) FILTER (WHERE sale_price IS NOT NULL) AS sold_items,
  ROUND(SUM(sale_price - purchase_price) FILTER (WHERE sale_price IS NOT NULL)::numeric, 0) AS total_profit,
  ROUND(AVG(sale_price - purchase_price) FILTER (WHERE sale_price IS NOT NULL)::numeric, 0) AS avg_profit
FROM journal_entries
```

#### Formulaire d'ajout
```json
// Query q_add_journal (REST API)
// POST /api/journal
{
  "item_title": "{{ input_title.value }}",
  "product_model_id": {{ select_model_journal.value ?? "null" }},
  "purchase_price": {{ input_purchase_price.value }},
  "sale_price": {{ input_sale_price.value || "null" }},
  "purchase_date": "{{ date_purchase.value }}",
  "sale_date": "{{ date_sale.value || null }}",
  "item_status": "{{ select_status_journal.value }}",
  "notes": "{{ input_notes.value }}"
}
```

#### Composants
| Composant | Type | Config |
|-----------|------|--------|
| `stat_profit` | Statistic | `{{ q_journal_stats.data[0]?.total_profit ?? 0 }}€` — Profit total |
| `stat_avg` | Statistic | `{{ q_journal_stats.data[0]?.avg_profit ?? 0 }}€` — Profit moyen |
| `stat_sold` | Statistic | `{{ q_journal_stats.data[0]?.sold_items }}/{{ q_journal_stats.data[0]?.total_items }}` — Articles vendus |
| `table_journal` | Table | Data: `{{ q_journal.data }}` — Editable: false — Colonnes: item_title, model_name, purchase_price, sale_price, profit (vert si >0), profit_pct |

---

### PAGE 6 — Retours Post-Vente

**Objectif** : Enregistrer le feedback après vente pour améliorer les prédictions.

#### Query `q_feedback_stats` (REST API)
- **Method** : GET
- **Endpoint** : `/api/feedback/stats`

#### Query `q_feedback_list` (PostgreSQL)
```sql
SELECT
  sf.id,
  pm.model_name,
  sf.listed_price,
  sf.actual_sale_price,
  sf.days_to_sell,
  sf.buyer_haggled,
  sf.notes,
  sf.created_at
FROM sales_feedback sf
JOIN product_models pm ON pm.id = sf.product_model_id
ORDER BY sf.created_at DESC
LIMIT 50
OFFSET {{ (table_feedback.pageIndex ?? 0) * 50 }}
```

#### Formulaire feedback
```json
// POST /api/feedback
{
  "product_model_id": {{ select_model_fb.value }},
  "listed_price": {{ input_listed.value }},
  "actual_sale_price": {{ input_actual.value }},
  "days_to_sell": {{ input_days.value }},
  "buyer_haggled": {{ toggle_haggled.value }},
  "notes": "{{ input_notes_fb.value }}"
}
```

#### Composants stat
| Composant | Config |
|-----------|--------|
| `stat_avg_days` | `{{ q_feedback_stats.data?.avg_days_to_sell }}j` — Délai moyen vente |
| `stat_haggle_rate` | `{{ Math.round(q_feedback_stats.data?.haggle_rate * 100) }}%` — Taux négociation |
| `stat_accuracy` | `{{ q_feedback_stats.data?.price_accuracy }}%` — Précision prix suggéré |

---

### PAGE 7 — Santé Système

**Objectif** : Monitoring du scraper, scheduler, et Gemini.

#### Query `q_health` (REST API)
- **Method** : GET `/api/health`
- **Polling** : 30s

#### Query `q_logs` (REST API)
- **Method** : GET `/api/health/logs?level={{ select_log_level.value }}&limit=100`
- **Polling** : 60s

#### Query `q_scheduler_stats` (PostgreSQL)
```sql
SELECT
  DATE_TRUNC('day', snapshot_at) AS day,
  COUNT(*) AS snapshots,
  AVG(total_results) AS avg_results,
  SUM(new_listings) AS new_listings,
  SUM(estimated_sold) AS estimated_sold
FROM search_snapshots
WHERE snapshot_at > NOW() - INTERVAL '7 days'
GROUP BY 1
ORDER BY 1 DESC
```

#### Query `q_error_stats` (PostgreSQL)
```sql
SELECT
  component,
  COUNT(*) AS error_count,
  MAX(created_at) AS last_error
FROM system_logs
WHERE level IN ('ERROR', 'CRITICAL')
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY component
ORDER BY error_count DESC
```

#### Composants
| Composant | Config |
|-----------|--------|
| `badge_status` | Status API: OK/degraded |
| `badge_gemini` | Circuit Gemini: Ouvert/Fermé |
| `text_last_snap` | Dernier snapshot: `{{ q_health.data.hours_since_last_snapshot }}h` |
| `select_log_level` | Options: INFO/WARNING/ERROR/CRITICAL |
| `table_logs` | `{{ q_logs.data }}` — Colonnes: created_at, level (badge), component, message |
| `chart_snapshots` | Bar chart — `{{ q_scheduler_stats.data }}` x: day, y: snapshots |
| `table_errors` | `{{ q_error_stats.data }}` — Composant + nb erreurs 24h |

---

### PAGE 8 — Paramètres

**Objectif** : Ajuster tous les seuils sans redéploiement, recalcul à la demande.

#### Query `q_settings` (REST API)
- **Method** : GET `/api/settings`
- **Run on load** : true

#### Formulaire — Section Poids de scoring
```json
// PUT /api/settings (q_save_weights)
{
  "updates": {
    "score_weight_repetition": {{ num_w_repetition.value }},
    "score_weight_velocity": {{ num_w_velocity.value }},
    "score_weight_rarity": {{ num_w_rarity.value }},
    "score_weight_lifespan": {{ num_w_lifespan.value }}
  }
}
```

**Validation côté Retool** (avant submit) :
```js
const sum = num_w_repetition.value + num_w_velocity.value + num_w_rarity.value + num_w_lifespan.value;
return sum === 100 ? null : `Somme des poids = ${sum} (doit être 100)`;
```

#### Helper pour pré-remplir les champs
```js
// defaultValue de chaque NumberInput :
// num_w_repetition → {{ q_settings.data?.find(s => s.key === 'score_weight_repetition')?.value ?? 35 }}
// num_w_velocity  → {{ q_settings.data?.find(s => s.key === 'score_weight_velocity')?.value ?? 25 }}
// etc.
```

#### Formulaire — Section Filtres qualité
```json
// PUT /api/settings (q_save_filters)
{
  "updates": {
    "level3_min_favourites": {{ num_l3_fav.value }},
    "level3_min_status": "{{ select_l3_status.value }}",
    "level4_min_favourites": {{ num_l4_fav.value }},
    "level4_min_status": "{{ select_l4_status.value }}",
    "level5_min_favourites": {{ num_l5_fav.value }},
    "level5_max_active_listings": {{ num_l5_max_active.value }}
  }
}
```

#### Formulaire — Section Prix
```json
// PUT /api/settings (q_save_prices)
{
  "updates": {
    "price_min_default": {{ num_price_min.value }},
    "price_max_default": {{ num_price_max.value }}
  }
}
```

#### Boutons admin
| Bouton | Action |
|--------|--------|
| `btn_reset` | POST `/api/settings/reset` → alert "Paramètres réinitialisés" → refresh q_settings |
| `btn_recalc` | POST `/api/admin/recalculate-scores` → alert résultat |

**Tableau récapitulatif de tous les paramètres** :
```
// Table en lecture seule pour voir la valeur actuelle de chaque paramètre
Data: {{ q_settings.data }}
Colonnes: key, value, default_value, description, value_type
```

---

## 4. Navigation inter-pages

Dans le Dashboard, sur clic d'une ligne du `table_models` :
```js
// Event: Row click
utils.openUrl('/apps/vinted-intelligence/vue-modele?model_id=' + table_models.selectedRow.id);
// OU si même app multi-pages :
navigateTo('Vue Modèle', { model_id: table_models.selectedRow.id });
```

---

## 5. Workflow Exploration de A à Z

1. Page Exploration → entrer "lemaire" + type "brand" + prix 50-300 + niveau 3
2. Cliquer **Lancer** → le polling démarre (statut toutes les 3s)
3. Après ~40s → statut "completed" → les clusters apparaissent
4. Sélectionner le cluster "Lemaire Castanet" → choisir la recherche → cliquer **Valider**
5. Le modèle est créé en base → aller dans Dashboard → le voir apparaître
6. Attendre le prochain snapshot automatique (3h) OU déclencher manuellement via le bouton "Snapshot" sur la page Recherches
