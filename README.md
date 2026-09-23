# 3D Hunter — WEB BETA 2.1 / Render Free

Ta paczka jest przygotowana specjalnie do darmowego wdrożenia na Render.

## Co działa
- interfejs 100% webowy,
- Smart Keywords PL → EN + synonimy,
- agregacja wielu źródeł,
- miniatury tam, gdzie źródła je udostępniają,
- ranking, filtry i skalowanie interfejsu,
- linkowalne wyszukiwania,
- opcjonalne Basic Auth dla zamkniętej bety.

## Co jest wyłączone na darmowym Render
Headless Chromium / Selenium. Darmowa instancja ma tylko 512 MB RAM, więc ten mechanizm byłby zbyt ciężki i niestabilny. Hunter korzysta tu z lżejszych adapterów API/indeksowych.

## Start
Przeczytaj `START_TUTAJ_RENDER.txt`. Najłatwiej wdrożyć przez `render.yaml` jako Render Blueprint.

## Health check
`/api/health`
