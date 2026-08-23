-- Migration 013: ajoute `home_trainer` au vocabulaire de core.threshold.power_source.
-- Aucune modification de 003 : les migrations appliquees ne se reecrivent pas
-- (le runner verifie leur checksum). Meme forme que 010 -- uniquement des COMMENT ON,
-- ni structure ni donnee ne changent.
--
-- POURQUOI -- la colonne n'a pas de contrainte CHECK : son vocabulaire ne vit que dans
-- ce commentaire, c'est-a-dire dans ce qu'un operateur lit avec \d+ core.threshold.
-- Le gabarit deploy/seed/athlete.sql.example annonce deja `home_trainer`, pas le
-- commentaire : deux sources de verite qui divergent, et rien en base pour trancher.
--
-- Le cas est reel et non theorique : l'athlete n'a pas de capteur de puissance sur le
-- velo, sa seule puissance mesuree vient du home-trainer. Un FTP mesure sur home-trainer
-- et un FTP mesure au capteur ne sont pas la meme grandeur -- meme raisonnement que
-- Garmin vs Stryd en course. D'ou la regle, inchangee : passer de l'un a l'autre CLOT la
-- periode de validite, on n'UPDATE jamais un seuil en place.

COMMENT ON COLUMN core.threshold.power_source IS
  'Origine de la mesure de puissance (garmin_native, stryd, powermeter_<modele>, home_trainer). VERIFIE (04 §3.2) : la Running Power Garmin est un modele proprietaire non documente ; « un 250 W Garmin et un 250 W Stryd ne sont pas la meme mesure ». Meme reserve pour home_trainer, ou la puissance est estimee par le simulateur et non mesuree au pedalier. Un changement de valeur ici DOIT clore la periode de validite et invalider le rFTP.';
