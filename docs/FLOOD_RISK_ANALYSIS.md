# Analyse du Risque d'Inondation - Moulin de la Lidoire

> Document de reference pour le calcul du risque d'inondation au moulin
> Coordonnees : 44.8759811, 0.0015091

> **Statut (octobre 2026)** : analyse initiale, conservee pour l'historique. Le modele
> implemente est `backend/app/services/risk_core.py` (v3.3), decrit dans le README et
> CLAUDE.md ; le pseudo-code et les poids ci-dessous ne sont plus a jour.
> Corrections : bassin versant de la Lidoire = 231 km² (SAGE Dordogne Atlantique), pas
> ~450 km² ; aucune station de hauteur/debit sur la Lidoire ; Pessac-sur-Dordogne est a
> environ 7 km en amont de la confluence.

---

## 1. Contexte Geographique

### 1.1 La Lidoire

| Caracteristique | Valeur |
|-----------------|--------|
| Longueur | 49,46 km |
| Bassin versant | ~450 km² |
| Altitude source | 116 m (foret landaise, Bosset) |
| Altitude confluence | 2 m |
| Pente moyenne | 2,30 m/km (tres faible) |
| Indice de Strahler | 5 |
| Nombre d'affluents | 11 |
| Principal affluent | Le Lechout |

**Caracteristiques importantes :**
- Pente tres faible = ecoulement lent = temps de reponse long aux precipitations
- Sinuosite elevee due a la faible pente
- Les 7 derniers km marquent la frontiere Gironde/Dordogne

### 1.2 Confluence avec la Dordogne

- **Localisation** : Entre Castillon-la-Bataille et Lamothe-Montravel
- **Position** : Rive droite de la Dordogne
- **Distance de Castillon** : ~1 km a l'est du centre

### 1.3 Communes traversees (19)

Bosset (source) → Saint-Meard-de-Gurcon → Montcaret → Saint-Michel-de-Montaigne → Lamothe-Montravel → Castillon-la-Bataille (confluence)

---

## 2. Influence de la Maree

### 2.1 Etendue de l'influence maregraphique

| Zone | Distance de l'embouchure |
|------|--------------------------|
| Embouchure Gironde | 0 km |
| Bordeaux | ~100 km |
| Libourne | ~120 km |
| Castillon-la-Bataille | ~140 km |
| **Flaujagues** (limite) | ~150 km |
| Pessac-sur-Dordogne (grandes marees) | ~160 km |

**La maree remonte jusqu'a Flaujagues**, en amont de Castillon et donc en amont de la confluence Lidoire/Dordogne.

### 2.2 Amplitude maregraphique

| Lieu | Maree faible | Maree moyenne | Grande maree |
|------|--------------|---------------|--------------|
| Embouchure | 1,5 m | 3,5 m | 5,5 m |
| Libourne | 2 m | 3 m | **4 m** |
| Zone confluence | 1,5 m | 2,5 m | 3,5 m |

**Phenomene d'amplification** : Le marnage s'amplifie en remontant l'estuaire (effet entonnoir).

### 2.3 Coefficients de maree

| Coefficient | Classification | Niveau de risque |
|-------------|----------------|------------------|
| 20-45 | Mortes-eaux faibles | Negligeable |
| 45-70 | Marees moyennes | Faible |
| 70-90 | Marees moyennes a fortes | Modere |
| **90-100** | **Vives-eaux** | **Eleve** |
| **100-110** | **Grandes vives-eaux** | **Tres eleve** |
| **110-120** | **Marees exceptionnelles** | **Maximal** |

**Seuils critiques :**
- **Coefficient 90** : Seuil a partir duquel le mascaret devient significatif
- **Coefficient 100** : Maree equinoxiale moyenne de reference
- **Coefficient 107** : Declenchement vigilance orange observe (fevrier 2024)
- **Coefficient 120** : Maximum theorique (~tous les 18 ans)

### 2.4 Le Mascaret

Onde de maree remontant la Dordogne lors des grandes marees.

| Caracteristique | Valeur |
|-----------------|--------|
| Vitesse | 15-30 km/h |
| Hauteur max | 1,5-2,5 m |
| Frequence | 730+/an (significatif ~50/an) |
| Limite amont | Genissac (Dordogne) |
| Coefficient minimum | >90 pour vague notable |

### 2.5 Asymetrie de la maree

- **Flot (montant)** : ~1/3 du cycle = courant fort
- **Jusant (descendant)** : ~2/3 du cycle = courant faible
- **Implication** : La maree montante est beaucoup plus puissante

---

## 3. Stations de Mesure Vigicrues

### 3.1 Station Libourne (principale)

| Information | Valeur |
|-------------|--------|
| Code station | **P577001001** |
| Riviere | Dordogne |
| Territoire | Gironde-Adour-Dordogne (32) |
| URL | https://www.vigicrues.gouv.fr/station/P577001001 |

**Donnees disponibles :**
- Hauteur d'eau temps reel
- Pas de debit (zone maregraphique)
- Previsions uniquement en periode de crue

**Niveaux typiques :**
- Normal : 0,5-1,5 m
- Variation maregraphique : +3-4 m selon coefficient

### 3.2 Stations amont

| Station | Code | Particularite |
|---------|------|---------------|
| Bergerac | P514001001 | Retenue du barrage, debit moyen 280 m³/s |
| Cenac-et-Saint-Julien | P238001001 | Donnees depuis 1900 |
| Alles-sur-Dordogne | P500000201 | - |

### 3.3 Station Lidoire

| Information | Valeur |
|-------------|--------|
| Code Sentival | **P56-0400** |
| URL | https://www.sentival.fr/stations-cours-eau?code=P56-0400 |

### 3.4 API Hub'Eau

**URL de base :** `https://hubeau.eaufrance.fr/api/v2/hydrometrie/`

**Endpoints principaux :**

```
# Observations temps reel (hauteur)
GET /observations_tr?code_entite=P577001001&grandeur_hydro=H&size=10

# Informations station
GET /referentiel/stations?code_station=P577001001

# Observations elaborees (moyennes)
GET /obs_elab?code_entite=P577001001
```

**Caracteristiques :**
- Mise a jour : toutes les 2 minutes
- Historique temps reel : 1 mois
- Formats : JSON, GeoJSON, CSV

---

## 4. Sources Meteo et Pluie

### 4.1 Open-Meteo (gratuit)

**Endpoint Meteo-France :**
```
https://api.open-meteo.com/v1/meteofrance
?latitude=44.8759811
&longitude=0.0015091
&hourly=precipitation,rain,temperature_2m
&forecast_days=7
```

**Donnees historiques :**
```
https://archive-api.open-meteo.com/v1/archive
?latitude=44.8759811
&longitude=0.0015091
&start_date=2024-01-01
&end_date=2024-01-31
&hourly=precipitation
```

### 4.2 Seuils de precipitation

#### Seuils Meteo-France (plaine)

| Alerte | Cumul 24h |
|--------|-----------|
| Jaune | 20-40 mm |
| Orange | 40-80 mm |
| Rouge | >80 mm |

#### Seuils adaptes bassin Lidoire

| Niveau | 24h | 48h | 72h |
|--------|-----|-----|-----|
| Vert | <20 mm | <30 mm | <40 mm |
| Jaune | 20-40 mm | 30-50 mm | 40-70 mm |
| Orange | 40-70 mm | 50-90 mm | 70-110 mm |
| Rouge | >70 mm | >90 mm | >110 mm |

**Facteur de saturation des sols :**
- Sols normaux : x1.0
- Sols humides : x1.25
- Sols satures : x1.5

---

## 5. Mecanisme d'Inondation au Moulin

### 5.1 Effet de blocage hydraulique

Quand la Dordogne est haute (crue et/ou maree), la Lidoire ne peut plus s'evacuer :

```
[Pluie sur bassin Lidoire]
         ↓
    [Lidoire monte]
         ↓
[Confluence avec Dordogne] ← [Dordogne haute = BLOCAGE]
         ↓
   [Refoulement amont]
         ↓
  [Inondation au moulin]
```

### 5.2 Scenarios de risque

| Scenario | Pluie Lidoire | Dordogne | Maree | Risque |
|----------|---------------|----------|-------|--------|
| A | Faible | Normale | Faible | Nul |
| B | Forte | Normale | Faible | Faible |
| C | Faible | Haute | Forte | Modere |
| D | Moyenne | Haute | Moyenne | Eleve |
| **E** | **Forte** | **Haute** | **Forte** | **Maximal** |

### 5.3 Timing critique

Le risque est maximal quand :
1. La Lidoire est chargee (pluie recente)
2. La Dordogne est en crue
3. La maree approche de la pleine mer

**Fenetre critique : PM-4h a PM+1h** (4 heures avant a 1 heure apres la pleine mer)

---

## 6. Formule de Calcul du Risque

### 6.1 Composantes du score (0-100)

| Composante | Poids | Description |
|------------|-------|-------------|
| Pluie 24h | 0-30 | Cumul sur bassin Lidoire |
| Niveau Dordogne | 0-25 | Hauteur a Libourne |
| Coefficient maree | 0-25 | Impact sur niveau max |
| Phase maree | 0-15 | Proximite pleine mer |
| Vent ouest | 0-5 | Surcote estuaire |

### 6.2 Algorithme

```python
def calcul_risque_moulin(
    pluie_24h_mm: float,
    pluie_72h_mm: float,
    niveau_dordogne_m: float,
    coef_maree: int,
    heures_avant_pm: float,  # negatif = apres PM
    vent_ouest_kmh: float = 0,
    saturation_sol: float = 1.0  # 1.0 a 1.5
) -> dict:
    """
    Calcule le score de risque d'inondation au moulin.

    Returns:
        dict avec score (0-100), niveau (VERT/JAUNE/ORANGE/ROUGE), facteurs
    """

    # === Score Pluie (0-30 points) ===
    pluie_ajustee = pluie_24h_mm * saturation_sol
    if pluie_ajustee < 20:
        score_pluie = (pluie_ajustee / 20) * 10
    elif pluie_ajustee < 50:
        score_pluie = 10 + ((pluie_ajustee - 20) / 30) * 10
    else:
        score_pluie = 20 + ((pluie_ajustee - 50) / 30) * 10
    score_pluie = min(30, score_pluie)

    # Bonus pluie 72h (si significative)
    if pluie_72h_mm > 80:
        score_pluie = min(30, score_pluie + 5)

    # === Score Dordogne (0-25 points) ===
    # Seuils : <2.5m=OK, 2.5-4m=vigilance, >4m=danger
    if niveau_dordogne_m < 2.5:
        score_dordogne = (niveau_dordogne_m / 2.5) * 5
    elif niveau_dordogne_m < 4.0:
        score_dordogne = 5 + ((niveau_dordogne_m - 2.5) / 1.5) * 10
    elif niveau_dordogne_m < 5.5:
        score_dordogne = 15 + ((niveau_dordogne_m - 4.0) / 1.5) * 10
    else:
        score_dordogne = 25
    score_dordogne = min(25, score_dordogne)

    # === Score Maree (0-25 points) ===
    # Seuils : <70=faible, 70-90=modere, 90-100=fort, >100=tres fort
    if coef_maree < 70:
        score_maree = (coef_maree / 70) * 5
    elif coef_maree < 90:
        score_maree = 5 + ((coef_maree - 70) / 20) * 10
    elif coef_maree < 105:
        score_maree = 15 + ((coef_maree - 90) / 15) * 7
    else:
        score_maree = 22 + ((coef_maree - 105) / 15) * 3
    score_maree = min(25, score_maree)

    # === Score Phase Maree (0-15 points) ===
    # Maximum a PM-2h, decroit vers PM-6h et PM+3h
    if heures_avant_pm < -3:  # Plus de 3h apres PM
        score_phase = 0
    elif heures_avant_pm > 6:  # Plus de 6h avant PM
        score_phase = 0
    else:
        # Pic a -2h (2h avant PM)
        distance_pic = abs(heures_avant_pm - (-2))
        score_phase = max(0, 15 * (1 - distance_pic / 5))

    # === Score Vent (0-5 points) ===
    # Vent ouest > 30 km/h = surcote
    if vent_ouest_kmh < 30:
        score_vent = 0
    elif vent_ouest_kmh < 60:
        score_vent = ((vent_ouest_kmh - 30) / 30) * 2
    elif vent_ouest_kmh < 100:
        score_vent = 2 + ((vent_ouest_kmh - 60) / 40) * 2
    else:
        score_vent = 4 + min(1, (vent_ouest_kmh - 100) / 50)
    score_vent = min(5, score_vent)

    # === Score Total ===
    score_total = score_pluie + score_dordogne + score_maree + score_phase + score_vent

    # === Classification ===
    if score_total < 25:
        niveau = "VERT"
        description = "Risque faible - Situation normale"
    elif score_total < 50:
        niveau = "JAUNE"
        description = "Vigilance - Surveiller l'evolution"
    elif score_total < 75:
        niveau = "ORANGE"
        description = "Risque significatif - Preparer protections"
    else:
        niveau = "ROUGE"
        description = "Risque majeur - Mettre en securite"

    return {
        "score": round(score_total, 1),
        "niveau": niveau,
        "description": description,
        "facteurs": {
            "pluie": round(score_pluie, 1),
            "dordogne": round(score_dordogne, 1),
            "maree": round(score_maree, 1),
            "phase": round(score_phase, 1),
            "vent": round(score_vent, 1)
        }
    }
```

### 6.3 Exemples de calcul

| Scenario | Pluie 24h | Dordogne | Coef | Phase | Score | Niveau |
|----------|-----------|----------|------|-------|-------|--------|
| Normal | 5 mm | 1.0 m | 50 | PM+5h | 8 | VERT |
| Pluie seule | 60 mm | 1.5 m | 55 | PM+4h | 28 | JAUNE |
| Grande maree | 10 mm | 2.0 m | 105 | PM-2h | 42 | JAUNE |
| Crue Dordogne | 25 mm | 4.5 m | 75 | PM-1h | 58 | ORANGE |
| **Concomitance** | 70 mm | 4.5 m | 100 | PM-2h | **82** | **ROUGE** |

---

## 7. Evenements Historiques de Reference

### 7.1 Crues majeures de la Dordogne

| Date | Type | Niveau Bergerac | Debit | Particularites |
|------|------|-----------------|-------|----------------|
| Dec 1944 | Crue centennale | 7,75 m | 3745 m³/s | Reference historique |
| Jan 1994 | Crue majeure | - | - | Concomitance maree |
| Dec 1999 | Tempete Martin | - | - | Coef 77 + surcote 2,32m |
| Fev 2010 | Tempete Xynthia | - | - | Coef 113 + surcote 0,98m |
| Dec 2023 | Crues regionales | - | - | Vigilance orange |

### 7.2 Lecons des evenements

**Tempete Martin (1999) - Cas d'ecole :**
- Coefficient seulement 77 (modere)
- Mais surcote de +2,32m a Ambes
- Vent 194 km/h
- Inondations 2h AVANT la pleine mer
- **Lecon** : Le coefficient seul ne suffit pas, la surcote est determinante

**Tempete Xynthia (2010) :**
- Coefficient 113 (fort)
- Surcote +0,98m au Verdon
- Vent 137 km/h
- **Lecon** : Grande maree + surcote moderee = danger majeur

---

## 8. Recommandations Operationnelles

### 8.1 Frequences de collecte

| Donnee | Frequence | Justification |
|--------|-----------|---------------|
| Niveau Dordogne | 15 min | Evolution rapide possible |
| Niveau Lidoire | 15 min | Si capteur disponible |
| Phase maree | 15 min | Calcul depuis horaires |
| Radar pluie | 15 min | Temps reel MF |
| Previsions pluie | 1h | Mise a jour modeles |
| Marees | 4x/jour | Predictibles, stables |
| Vent | 1h | Previsions |

### 8.2 Seuils d'alerte

| Niveau | Score | Actions |
|--------|-------|---------|
| VERT | <25 | Monitoring standard |
| JAUNE | 25-49 | Notification, surveillance accrue |
| ORANGE | 50-74 | Alerte, preparation protections |
| ROUGE | ≥75 | Alerte urgente, mise en securite |

### 8.3 Periodes de vigilance accrue

- **Grandes marees** : Coefficient > 95
- **Automne/Hiver** : Octobre a Mars (crues)
- **Marees d'equinoxe** : Mars et Septembre
- **Apres pluies prolongees** : Sols satures

---

## 9. Sources et References

### APIs et donnees

- **Vigicrues** : https://www.vigicrues.gouv.fr/
- **Hub'Eau Hydrometrie** : https://hubeau.eaufrance.fr/page/api-hydrometrie
- **Sentival** : https://www.sentival.fr/
- **SHOM Marees** : https://maree.shom.fr/
- **Maree.info Bordeaux** : https://maree.info/161
- **Open-Meteo** : https://open-meteo.com/
- **DPF Dordogne** : https://dpf-dordogne.fr/

### Documentation

- SAGE Dordogne Atlantique : https://dordogne-atlantique.fr/
- SMIDDEST (estuaire Gironde) : https://www.smiddest.fr/
- Prefecture Gironde - Risques : https://www.gironde.gouv.fr/

### Articles scientifiques

- SCHAPI : Methodes de prevision des crues
- AIGA : Systeme d'alerte inondation
- Etudes concomitance maree/crue (NHESS)

---

*Document genere le 26/01/2026 - A mettre a jour selon retours d'experience*
