# crossex_spread — recherche sur le spread arbitrage Gate CrossEx

Scripts de recherche pour évaluer la stratégie « CrossEx Futures Spread Arbitrage »
de la page https://www.gate.com/crossex/rate. Ils utilisent uniquement des données
publiques : aucune clé API n'est nécessaire.

## Ce que calcule la page

`Max APR = (prix_max − prix_min) / prix_min × 365 / N`, avec N la période choisie (14D → ×26,07).
La formule a été vérifiée sur les 8 lignes de la capture. Rouge = place la moins chère (jambe longue),
vert = place la plus chère (jambe courte). Les prix sont des prix *mark*, pas le bid/ask exécutable.
Le calcul ignore les frais, le funding, le slippage et le cas où l'écart ne se referme pas.

## Fichiers

| Script | Rôle |
|---|---|
| `venues.py` | Adaptateurs de données publiques : Gate, Binance, Aster, OKX, Hyperliquid (dont les DEX HIP-3), Lighter, Bybit. |
| `snapshot.py` | Photo live des 8 actifs de la capture : carnet, VWAP à 5 k$, mark, index, funding par intervalle et par jour. |
| `history.py` | 45 jours de bougies 1h (dernier prix + mark) et l'historique de funding. Bybit est reconstruit depuis `public.bybit.com`. |
| `analyze.py` | Statistiques de spread par paire (moyenne, écart-type, demi-vie AR(1)), funding cumulé, backtests nets de frais. |
| `checks.py` | Contrôles de robustesse : mark vs dernier prix, sessions de la bourse de HK (XIAOMI), funding hebdomadaire. |
| `scan_all.py` | Scan de tous les actifs cotés sur au moins 2 places liquides : spread exécutable bid/ask, frais et funding. |

```bash
pip install -r crossex_spread/requirements.txt
python crossex_spread/snapshot.py      # télécharge aussi la liste des symboles CrossEx (endpoint public)
python crossex_spread/history.py       # DAYS=45 par défaut
python crossex_spread/analyze.py
python crossex_spread/checks.py
python crossex_spread/scan_all.py 1e6  # filtre : volume 24h ≥ 1 M$ sur chaque jambe
```

Les sorties sont écrites dans `crossex_spread/data/`, ignoré par git.

## Limites connues

- L'API REST de Bybit est géo-bloquée depuis une IP US. `venues.bybit()` devrait fonctionner
  depuis l'Europe, mais il n'a pas été testé. Sans lui, le funding Bybit live vient du flux agrégé
  de Lighter (normalisé sur 8h) et l'historique Bybit ne contient que les prix.
- Les backtests utilisent les clôtures horaires, donc le dernier trade de l'heure. Sur les places
  illiquides, ce prix fabrique de faux spreads. Il faut comparer avec la version mark (`checks.py`),
  et un vrai backtest demanderait l'historique bid/ask.
- Les contrats USDC (UMX) d'OKX ne sont pas exposés par l'API publique v5 d'OKX.
- L'API CrossEx `GET /crossex/market/tickers` exige une signature et ne renvoie ni bid ni ask.
  Les carnets doivent donc venir de chaque exchange.

## Conclusions principales (données du 24/11/2026)

1. Le spread arbitrage est **la même position** que le funding arb : long un perp et short le même
   perp sur une autre place. P&L = (spread d'entrée − spread de sortie) + Σ(funding short − funding long)
   − frais − slippage. Les deux termes doivent être évalués ensemble.
2. Les écarts reviennent vers **leur propre moyenne** avec une demi-vie de 0,4 à 4 h. Ils ne se
   referment pas « en 14 jours », et plusieurs ne reviennent jamais vers 0, car leur écart moyen est
   structurel : MOONSHOT Bybit/Binance ≈ +1,05 %, JIO HL/Gate ≈ +1,96 %.
3. JIO (+36 % « APR » sur la page) : le short Hyperliquid paie environ 0,5 %/jour de funding.
   Tenir 14 jours ⇒ ≈ −7 % en médiane.
4. Sur les places illiquides, une grande partie des « opportunités » est du bruit de dernier prix.
   Exemple, CYBER Aster→Binance : 108 signaux avec le dernier prix contre 23 avec le mark.
5. Sur les actifs liquides, environ 30 paires dépassent 0,20 % (les frais taker aller-retour) à un
   instant donné, et presque aucune ne dépasse 0,5 %.
