-- Migration 010: precise SUR QUOI porte raw.fit_file.sha256, et documente le scan de
-- reconciliation que le commentaire de table de 002 promet depuis le debut.
-- Aucune modification de 002 : les migrations appliquees ne se reecrivent pas
-- (le runner verifie leur checksum).
--
-- Ni structure ni donnee ne changent ici : uniquement des COMMENT ON. C'est deliberement
-- une migration a part entiere plutot qu'une note dans un fichier Markdown, parce que ces
-- deux commentaires sont exactement ce qu'un operateur lit avec \d+ raw.fit_file au moment
-- ou il doit decider si une ligne est fiable.
--
-- POURQUOI -- le commentaire de 002 disait « sha256 du binaire .FIT », ce qui laissait
-- croire que hacher le telechargement suffisait. Or le telechargement « original » de
-- Garmin est une ARCHIVE ZIP contenant le .fit ou le .tcx. Le code a effectivement compare
-- le sha de l'ENVELOPPE a celui du MEMBRE stocke ici : les deux ne pouvaient jamais
-- coincider, le court-circuit d'idempotence ne se declenchait donc jamais, et chaque
-- passage sur la fenetre du jour courant reecrivait le PVC. Defaut reel, corrige dans
-- garminator.fit.archive / garminator.scheduler.activity_files. Le commentaire ci-dessous
-- rend la regle explicite pour qu'elle ne soit plus a redecouvrir.

COMMENT ON COLUMN raw.fit_file.sha256 IS
  'sha256 du CONTENU ARCHIVE -- c''est-a-dire du fichier .fit/.tcx lui-meme, JAMAIS de l''archive ZIP dans laquelle Garmin le sert. Calcule par garminator.fit.archive.archive_bytes APRES relecture du fichier ecrit sur le PVC (pas sur les octets en memoire), ce qui detecte une corruption survenue a l''ecriture elle-meme. UNIQUE : un meme contenu n''est archive qu''une fois. C''est le pivot de la detection de re-telechargement effectif (§6.2), et tout appelant qui veut savoir « ce contenu est-il deja en base ? » DOIT hacher le membre extrait (garminator.fit.archive.extract_payload), pas le telechargement brut -- confondre les deux a deja produit un defaut reel. Distinct du hash du resume JSON (§6.3).';

COMMENT ON COLUMN raw.fit_file.storage_path IS
  'Chemin ABSOLU du fichier sur le PVC, tel qu''il etait monte a l''ecriture. Le nom de fichier porte les 12 premiers hex de sha256 : une nouvelle version d''une meme activite (fichier tronque au premier telechargement, complete ensuite) obtient donc un nom distinct et n''ECRASE JAMAIS la precedente. Corollaire : un conteneur qui monte le PVC ailleurs verra toutes les lignes comme « fichier absent » -- c''est le symptome attendu, pas une perte de donnees.';

COMMENT ON TABLE raw.fit_file IS
  'Metadonnees des fichiers .FIT archives sur le PVC. Le binaire N''EST PAS en base (pas de bytea) : ~1 090 fichiers de plusieurs Mo gonfleraient TOAST et chaque dump PostgreSQL sans rien apporter de requetable. REGLE D''ATOMICITE IMPERATIVE : le fichier est ecrit ET fsync sur le PVC AVANT l''INSERT de cette ligne, jamais l''inverse. Un fichier orphelin (sans ligne) est inoffensif et rattrapable par un scan ; une ligne orpheline (sans fichier) rend la reconstructibilite de core fausse. Ce scan EXISTE desormais : `python -m garminator.ops reconcile --archive-root <montage>` (module garminator.ops.reconcile, CronJob ops-reconcile, PVC monte en lecture seule, aucune suppression automatique) -- voir docs/RUNBOOK.md §9. Corollaire de sauvegarde : le dump PostgreSQL et le snapshot du PVC doivent etre pris au MEME point dans le temps (cf. §6.5).';
