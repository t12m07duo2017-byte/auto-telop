#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prproj_generate.py
===================

align_script.py の出力(タイムスタンプ+スタイル名+SE)を読み込み、
yamaさんテンプレ.prproj を複製して新しいナレーション音声・テロップ・SEを
差し込んだ新しい .prproj を書き出す。

.prprojの読み取りはElementTree/正規表現で自由に行うが、書き込みは
既存部分を一切reserializeせず、生テキストへの正規表現ピンポイント
挿入/置換のみで行う(check_span_splice.pyで安全性を検証済みの方式)。

新規オブジェクトは「テンプレ内に既に存在する同種オブジェクトの
XMLブロックを丸ごと複製し、ObjectID/ObjectUIDだけ新規に振り直す」
(prproj_common.GraphCloner)方式で作る。これはPremiere自身が同じ
MasterClipを複数の配置から使い回しているのと同じ考え方で、
未知のバイナリフィールドを手書きで再構築するより安全。

現状はStage1(ナレーション音声のインポート+配置)とStage2(テロップ
クリップの複製・差し込み)を実装している。
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys

from .prproj_common import (
    load_prproj_text, save_prproj_text, ObjectIdAllocator, GraphCloner,
    new_guid, sec_to_ticks, SECONDS_PER_TICK,
    find_block_by_object_id, find_block_by_object_uid,
)
from .prproj_text_codec import replace_source_text

TEMPLATE_DEFAULT = None  # CLIで必須指定

AUDIO_EXTENSIONS = (".mp3", ".wav", ".aac", ".m4a", ".aif", ".aiff")


# ---------------------------------------------------------------------------
# テンプレ自動検出(特定案件専用のUIDをハードコードせず、任意のテンプレ
# .prprojから同じ役割のオブジェクトを都度探す)
# ---------------------------------------------------------------------------

def find_sequence_uid(text: str, sequence_name: str = None) -> str:
    """テンプレ内のSequenceを探す。sequence_name指定時はその名前のもの、
    省略時は最初に見つかったものを使う(通常はテンプレに1つしか無い)。"""
    for m in re.finditer(r'<Sequence ObjectUID="([0-9a-fA-F-]{36})"[^>]*>.*?</Sequence>', text, re.S):
        block = m.group(0)
        if sequence_name:
            name_m = re.search(r"<Name>([^<]*)</Name>", block)
            if not name_m or name_m.group(1) != sequence_name:
                continue
        return m.group(1)
    raise ValueError("テンプレ内にSequenceが見つかりません" + (f" (name={sequence_name})" if sequence_name else ""))


def probe_frame_rate_fps(text: str, sequence_uid: str) -> float:
    """VideoTrackGroupのFrameRateタグ(ticks/フレーム)からfpsを逆算する。
    見つからない場合は29.97fps(1001/30000)にフォールバックする。"""
    group_id = _find_track_group_object_id(text, sequence_uid, "VideoTrackGroup")
    group_span = find_block_by_object_id(text, "VideoTrackGroup", group_id)
    if group_span is not None:
        m = re.search(r"<FrameRate>(\d+)</FrameRate>", text[group_span[0]:group_span[1]])
        if m:
            ticks_per_frame = int(m.group(1))
            if ticks_per_frame > 0:
                return SECONDS_PER_TICK / ticks_per_frame
    return 30000 / 1001


def _find_track_group_object_id(text: str, sequence_uid: str, want_tag: str) -> str:
    """SequenceのTrackGroupsから、実体が want_tag ("VideoTrackGroup"/
    "AudioTrackGroup") であるものを返す。"First"のメディア種別GUIDは
    Premiere内部の固定値の可能性が高いが確証が無いため、実際に参照先の
    タグ名を見て判定する(テンプレ非依存で確実)。"""
    seq_span = find_block_by_object_uid(text, "Sequence", sequence_uid)
    if seq_span is None:
        raise ValueError(f"Sequenceが見つかりません: {sequence_uid}")
    seq_block = text[seq_span[0]:seq_span[1]]
    for m in re.finditer(r'<Second ObjectRef="(\d+)"/>', seq_block):
        obj_id = m.group(1)
        tag_m = re.search(rf'<{want_tag} ObjectID="{obj_id}"', text)
        if tag_m:
            return obj_id
    raise ValueError(f"該当する{want_tag}が見つかりません")


def find_donor_audio_clip_name(text: str) -> str:
    """プロジェクトのビン内から、音声インポートの構造ドナーとして使える
    既存の音声クリップ名(拡張子で判定)を1つ探す。どのファイルが
    選ばれても、複製時にタイトル/パス/尺は全て新規音声のものに
    書き換えるため、選択自体に意味は無い。"""
    for m in re.finditer(r"<Name>([^<]*)</Name>\s*</ProjectItem>\s*<MasterClip ObjectURef=", text):
        name = m.group(1)
        if name.lower().endswith(AUDIO_EXTENSIONS):
            return name
    raise ValueError(
        "テンプレのビン内に音声クリップ(構造ドナーとして使えるもの)が"
        "見つかりません。テンプレのプロジェクトパネルに1つ以上、"
        "音声ファイル(mp3/wav等)をインポート済みの状態で保存してください。"
    )


def _track_has_items(text: str, track_tag: str, track_uid: str) -> bool:
    span = find_block_by_object_uid(text, track_tag, track_uid)
    if span is None:
        return True  # 見つからない場合は安全側(使用中扱い)に倒す
    block = text[span[0]:span[1]]
    m = re.search(r"<TrackItems[^>]*>(.*?)</TrackItems>", block, re.S)
    if m is None:
        return False
    return bool(re.search(r"<TrackItem ", m.group(1)))


def find_empty_track_uids(text: str, group_object_id: str, group_tag: str, track_tag: str, count: int) -> list:
    """指定TrackGroup(group_tag: "VideoTrackGroup"/"AudioTrackGroup")内で、
    TrackItemが1つも無い(=空の)トラックをIndex昇順でcount個探して返す
    (ObjectUIDのリスト)。"""
    group_span = find_block_by_object_id(text, group_tag, group_object_id)
    if group_span is None:
        raise ValueError(f"TrackGroupが見つかりません: {group_object_id}")
    group_block = text[group_span[0]:group_span[1]]
    tracks = re.findall(r'<Track Index="(\d+)" ObjectURef="([0-9a-fA-F-]{36})"/>', group_block)
    tracks.sort(key=lambda t: int(t[0]))
    found = []
    for _, uid in tracks:
        if not _track_has_items(text, track_tag, uid):
            found.append(uid)
            if len(found) >= count:
                break
    if len(found) < count:
        raise ValueError(
            f"{track_tag}に空きトラックが{count}本必要ですが{len(found)}本しか"
            f"見つかりませんでした。テンプレのシーケンスに空の{track_tag}を"
            f"追加で用意してください。"
        )
    return found


def find_empty_video_track_donor_uid(text: str, sequence_uid: str) -> str:
    group_id = _find_track_group_object_id(text, sequence_uid, "VideoTrackGroup")
    return find_empty_track_uids(text, group_id, "VideoTrackGroup", "VideoClipTrack", 1)[0]

# Premiere標準ラベルカラー名 -> BE.Prefs.LabelColors.N (Contents/Settings/EveScripts/
# NewMenus/HandlerTimeline_AVClip_Menu.eve の cmd.edit.label.N から確認済み)
LABEL_COLOR_INDEX = {
    "Violet": 0, "Iris": 1, "Caribbean": 2, "Lavender": 3, "Cerulean": 4,
    "Forest": 5, "Rose": 6, "Mango": 7, "Purple": 8, "Blue": 9,
    "Teal": 10, "Magenta": 11, "Tan": 12, "Green": 13, "Brown": 14, "Yellow": 15,
}

BASE_CAPTION_STYLE_NAME = "yamaさん基本"
CATEGORY_COLOR_LABEL = {
    "ポジティブ": LABEL_COLOR_INDEX["Rose"],
    "ネガティブ": LABEL_COLOR_INDEX["Blue"],
    "強調テロップ": LABEL_COLOR_INDEX["Mango"],
}
BASE_CAPTION_COLOR_LABEL = LABEL_COLOR_INDEX["Lavender"]

# 静止画クリップのドナー一式(ユーザー自身の実プロジェクトから抽出した、
# 実際に画像がインポート・配置された状態のブロック26個)。
# yamaさんテンプレ.prproj自体にはインポート済みの静止画クリップが1つも
# 無いため、テロップ/SEのように同一ファイル内から複製元を探せない。
# そのため専用の小さなドナーフラグメントファイルを別途用意している。
IMAGE_CLIP_DONOR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "image_clip_donor.xml")


def _tag_value(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", block)
    if not m:
        raise ValueError(f"<{tag}> が見つかりません")
    return m.group(1)


def _tag_ref(block: str, tag: str) -> str:
    m = re.search(rf'<{tag} ObjectRef="(\d+)"', block)
    if m:
        return m.group(1)
    m = re.search(rf'<{tag} ObjectURef="([0-9a-fA-F-]{{36}})"', block)
    if m:
        return m.group(1)
    raise ValueError(f"<{tag} ObjectRef.../> が見つかりません")


def probe_duration_seconds(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def find_donor_masterclip(text: str, name: str) -> dict:
    """名前(例: 'チリン.mp3')からビン項目→MasterClipの構造情報を辿って返す。"""
    m = re.search(
        r'<ClipProjectItem ObjectUID="([0-9a-fA-F-]{36})"[^>]*>.*?<Name>' + re.escape(name) +
        r'</Name>.*?<MasterClip ObjectURef="([0-9a-fA-F-]{36})"/>\s*</ClipProjectItem>',
        text, re.S,
    )
    if not m:
        raise ValueError(f"ドナークリップが見つかりません: {name}")
    clip_project_item_uid = m.group(1)
    master_clip_uid = m.group(2)

    master_block = find_block_by_object_uid(text, "MasterClip", master_clip_uid)
    master_block = text[master_block[0]:master_block[1]]

    logging_info_id = _tag_ref(master_block, "LoggingInfo")
    audio_component_chain_id = re.search(r'<AudioComponentChain Index="0" ObjectRef="(\d+)"/>', master_block).group(1)
    audio_clip_id = re.search(r'<Clip Index="0" ObjectRef="(\d+)"/>', master_block).group(1)
    channel_groups_id = _tag_ref(master_block, "AudioClipChannelGroups")
    def_mapping_id = _tag_value(master_block, "DefMappingID")

    audio_clip_block = find_block_by_object_id(text, "AudioClip", audio_clip_id)
    audio_clip_block = text[audio_clip_block[0]:audio_clip_block[1]]
    markers_id = _tag_ref(audio_clip_block, "Markers")
    source_id = _tag_ref(audio_clip_block, "Source")
    secondary0_id = re.search(r'<SecondaryContentItem Index="0" ObjectRef="(\d+)"/>', audio_clip_block).group(1)
    secondary1_id = re.search(r'<SecondaryContentItem Index="1" ObjectRef="(\d+)"/>', audio_clip_block).group(1)

    media_source_block = find_block_by_object_id(text, "AudioMediaSource", source_id)
    media_source_block = text[media_source_block[0]:media_source_block[1]]
    media_uid = _tag_ref(media_source_block, "Media")

    media_block = find_block_by_object_uid(text, "Media", media_uid)
    media_block = text[media_block[0]:media_block[1]]
    audio_stream_id = _tag_ref(media_block, "AudioStream")
    implementation_id = _tag_value(media_block, "ImplementationID")
    conformed_audio_rate = _tag_value(media_block, "ConformedAudioRate")

    audio_stream_block = find_block_by_object_id(text, "AudioStream", audio_stream_id)
    audio_stream_block = text[audio_stream_block[0]:audio_stream_block[1]]
    sample_type_default = _tag_value(audio_stream_block, "SampleType")

    channel_groups_block = find_block_by_object_id(text, "ClipChannelGroupVectorSerializer", channel_groups_id)
    channel_groups_block = text[channel_groups_block[0]:channel_groups_block[1]]
    channel_vector_id = re.search(r'<ClipChannelVectorItem Index="0" ObjectRef="(\d+)"/>', channel_groups_block).group(1)

    channel_vector_block = find_block_by_object_id(text, "ClipChannelVectorSerializer", channel_vector_id)
    channel_vector_block = text[channel_vector_block[0]:channel_vector_block[1]]
    ch0_id = re.search(r'<ClipChannelItem Index="0" ObjectRef="(\d+)"/>', channel_vector_block).group(1)
    ch1_id = re.search(r'<ClipChannelItem Index="1" ObjectRef="(\d+)"/>', channel_vector_block).group(1)

    return dict(
        clip_project_item_uid=clip_project_item_uid,
        master_clip_uid=master_clip_uid,
        logging_info_id=logging_info_id,
        audio_component_chain_id=audio_component_chain_id,
        audio_clip_id=audio_clip_id,
        channel_groups_id=channel_groups_id,
        def_mapping_id=def_mapping_id,
        markers_id=markers_id,
        source_id=source_id,
        secondary0_id=secondary0_id,
        secondary1_id=secondary1_id,
        media_uid=media_uid,
        audio_stream_id=audio_stream_id,
        implementation_id=implementation_id,
        conformed_audio_rate=conformed_audio_rate,
        sample_type_default=sample_type_default,
        channel_vector_id=channel_vector_id,
        ch0_id=ch0_id,
        ch1_id=ch1_id,
    )


def import_narration_audio(text: str, allocator: ObjectIdAllocator, audio_path: str,
                            donor_audio_name: str = None) -> tuple:
    """新しい音声ファイルをドナー構造の複製でビンにインポートする。

    戻り値: (更新後のtext, infoディクショナリ)
    infoには後続のplace_narration_on_track/クローン処理で再利用する
    master_clip_uid・markers_id・source_id・duration_ticks・display_name を含む。
    donor_audio_name省略時はテンプレのビンから自動で1つ選ぶ。
    """
    donor_audio_name = donor_audio_name or find_donor_audio_clip_name(text)
    donor = find_donor_masterclip(text, donor_audio_name)
    display_name = os.path.basename(audio_path)
    duration_sec = probe_duration_seconds(audio_path)
    duration_ticks = sec_to_ticks(duration_sec)
    is_wav = audio_path.lower().endswith(".wav")
    sample_type = "3" if is_wav else donor["sample_type_default"]

    cloner = GraphCloner(text, allocator)
    cloner.plan("ClipProjectItem", donor["clip_project_item_uid"], by_uid=True)
    cloner.plan("MasterClip", donor["master_clip_uid"], by_uid=True)
    cloner.plan("ClipLoggingInfo", donor["logging_info_id"])
    cloner.plan("AudioComponentChain", donor["audio_component_chain_id"])
    cloner.plan("AudioClip", donor["audio_clip_id"])
    cloner.plan("Markers", donor["markers_id"])
    cloner.plan("SecondaryContent", donor["secondary0_id"])
    cloner.plan("SecondaryContent", donor["secondary1_id"])
    cloner.plan("AudioMediaSource", donor["source_id"])
    cloner.plan("Media", donor["media_uid"], by_uid=True)
    cloner.plan("AudioStream", donor["audio_stream_id"])
    cloner.plan("ClipChannelGroupVectorSerializer", donor["channel_groups_id"])
    cloner.plan("ClipChannelVectorSerializer", donor["channel_vector_id"])
    cloner.plan("ClipChannelSerializer", donor["ch0_id"])
    cloner.plan("ClipChannelSerializer", donor["ch1_id"])

    blocks = cloner.render()

    new_clip_id = new_guid()
    new_content_state = new_guid()
    new_file_key = new_guid()

    patched = []
    for block in blocks:
        if block.startswith("<ClipProjectItem"):
            block = re.sub(r"<Name>[^<]*</Name>", f"<Name>{display_name}</Name>", block)
        elif block.startswith("<MasterClip"):
            block = re.sub(r"<Name>[^<]*</Name>", f"<Name>{display_name}</Name>", block)
            # TranscriptClip(Index=1)は複製していないため参照を落とす
            block = re.sub(
                r'\s*<Clip Index="1" ObjectRef="\d+"/>', "", block)
        elif block.startswith("<ClipLoggingInfo"):
            block = re.sub(r"<ClipName>[^<]*</ClipName>", f"<ClipName>{display_name}</ClipName>", block)
            block = re.sub(r"<MediaOutPoint>\d+</MediaOutPoint>", f"<MediaOutPoint>{duration_ticks}</MediaOutPoint>", block)
            block = re.sub(r"\s*<LogNote>[0-9A-Fa-f]*</LogNote>", "", block)
        elif block.startswith("<AudioClip"):
            block = re.sub(r"<ClipID>[0-9a-fA-F-]{36}</ClipID>", f"<ClipID>{new_clip_id}</ClipID>", block)
        elif block.startswith("<Markers"):
            block = re.sub(r"<LastContentState>[0-9a-fA-F-]{36}</LastContentState>",
                            f"<LastContentState>{new_content_state}</LastContentState>", block)
        elif block.startswith("<AudioMediaSource"):
            block = re.sub(r"<OriginalDuration>\d+</OriginalDuration>",
                            f"<OriginalDuration>{duration_ticks}</OriginalDuration>", block)
        elif block.startswith("<Media "):
            block = re.sub(r"<Title>[^<]*</Title>", f"<Title>{display_name}</Title>", block)
            block = re.sub(r"<FileKey>[0-9a-fA-F-]{36}</FileKey>", f"<FileKey>{new_file_key}</FileKey>", block)
            block = re.sub(r"<ContentAndMetadataState>[0-9a-fA-F-]{36}</ContentAndMetadataState>",
                            f"<ContentAndMetadataState>{new_content_state}</ContentAndMetadataState>", block)
            block = _patch_modification_state(block, new_content_state)
            abs_path = os.path.abspath(audio_path)
            block = re.sub(r"\s*<RelativePath>.*?</RelativePath>", "", block)
            block = re.sub(r"<ActualMediaFilePath>[^<]*</ActualMediaFilePath>",
                            f"<ActualMediaFilePath>{abs_path}</ActualMediaFilePath>", block)
            block = re.sub(r"<FilePath>[^<]*</FilePath>", f"<FilePath>{abs_path}</FilePath>", block)
            block = re.sub(r"\s*<MediaFileHistory1>.*?</MediaFileHistory1>", "", block)
            block = re.sub(r"\s*<MediaFileHistory0>.*?</MediaFileHistory0>", "", block)
        elif block.startswith("<AudioStream"):
            block = re.sub(r"<SampleType>\d+</SampleType>", f"<SampleType>{sample_type}</SampleType>", block)
            block = re.sub(r"\s*<PeakFilePath>.*?</PeakFilePath>", "", block)
            block = re.sub(r"<Duration>\d+</Duration>", f"<Duration>{duration_ticks}</Duration>", block)
        patched.append(block)

    fragment = "\n".join(patched) + "\n"
    text = text.replace("</PremiereData>", fragment + "</PremiereData>")

    # プロジェクトパネル(Root Bin)にも見えるようにする
    new_bin_item_uid = cloner.guid_map[donor["clip_project_item_uid"]]
    text = re.sub(
        r'(<Item Index="\d+" ObjectURef="[0-9a-fA-F-]{36}"/>\s*)(</Items>\s*</ProjectItemContainer>\s*</RootProjectItem>)',
        lambda m: m.group(1) + _new_bin_item_line(text, new_bin_item_uid) + m.group(2),
        text, count=1,
    )

    new_master_clip_uid = cloner.guid_map[donor["master_clip_uid"]]
    new_markers_id = cloner.id_map[donor["markers_id"]]
    new_source_id = cloner.id_map[donor["source_id"]]
    return text, dict(
        master_clip_uid=new_master_clip_uid,
        markers_id=new_markers_id,
        source_id=new_source_id,
        duration_ticks=duration_ticks,
        display_name=display_name,
        donor_master_clip_uid=donor["master_clip_uid"],
        donor_name=donor_audio_name,
    )


# ---------------------------------------------------------------------------
# トラックへの配置 (SubClip + AudioClipTrackItem)
# ---------------------------------------------------------------------------

def find_donor_placement(text: str, donor_master_clip_uid: str, donor_name: str) -> dict:
    """donor_master_clip_uidを参照する既存のSubClip配置(タイムライン上の
    1インスタンス)を1つ見つけ、その構造情報を返す。"""
    m = re.search(
        r'<SubClip ObjectID="(\d+)"[^>]*>\s*<Clip ObjectRef="(\d+)"/>\s*'
        rf'<MasterClip ObjectURef="{re.escape(donor_master_clip_uid)}"/>.*?<Name>' +
        re.escape(donor_name) + r"</Name>\s*</SubClip>",
        text, re.S,
    )
    if not m:
        raise ValueError(f"配置インスタンスのドナーが見つかりません: {donor_name}")
    subclip_id = m.group(1)
    audio_clip_instance_id = m.group(2)

    track_item_id = None
    track_item_block = None
    needle = f'<SubClip ObjectRef="{subclip_id}"/>'
    for m3 in re.finditer(r'<AudioClipTrackItem ObjectID="(\d+)"[^>]*>.*?</AudioClipTrackItem>', text, re.S):
        if needle in m3.group(0):
            track_item_id = m3.group(1)
            track_item_block = m3.group(0)
            break
    if track_item_id is None:
        raise ValueError("AudioClipTrackItemが見つかりません")
    components_id = re.search(r'<Components ObjectRef="(\d+)"/>', track_item_block).group(1)

    instance_clip_block = find_block_by_object_id(text, "AudioClip", audio_clip_instance_id)
    instance_clip_block = text[instance_clip_block[0]:instance_clip_block[1]]
    shared_markers_id = _tag_ref(instance_clip_block, "Markers")
    shared_source_id = _tag_ref(instance_clip_block, "Source")
    sec0_id = re.search(r'<SecondaryContentItem Index="0" ObjectRef="(\d+)"/>', instance_clip_block).group(1)
    sec1_id = re.search(r'<SecondaryContentItem Index="1" ObjectRef="(\d+)"/>', instance_clip_block).group(1)

    return dict(
        track_item_id=track_item_id,
        components_id=components_id,
        subclip_id=subclip_id,
        audio_clip_instance_id=audio_clip_instance_id,
        shared_markers_id=shared_markers_id,
        shared_source_id=shared_source_id,
        sec0_id=sec0_id,
        sec1_id=sec1_id,
    )


def place_audio_on_track(text: str, allocator: ObjectIdAllocator, track_uid: str,
                          master_clip_uid: str, markers_id: str, source_id: str,
                          display_name: str, start_ticks: int, duration_ticks: int,
                          donor_master_clip_uid: str, donor_name: str) -> str:
    """既存MasterClip(master_clip_uid、markers_id/source_idは内部Clipの
    再利用先)をタイムライン上の指定位置に配置する。SE/ナレーション共通。
    """
    donor = find_donor_placement(text, donor_master_clip_uid, donor_name)

    cloner = GraphCloner(text, allocator)
    cloner.plan("AudioClipTrackItem", donor["track_item_id"])
    cloner.plan("AudioComponentChain", donor["components_id"])
    cloner.plan("SubClip", donor["subclip_id"])
    cloner.plan("AudioClip", donor["audio_clip_instance_id"])
    cloner.plan("SecondaryContent", donor["sec0_id"])
    cloner.plan("SecondaryContent", donor["sec1_id"])
    blocks = cloner.render()

    new_track_item_id = cloner.id_map[donor["track_item_id"]]
    new_clip_id = new_guid()
    new_item_guid = new_guid()

    patched = []
    for block in blocks:
        if block.startswith("<AudioClipTrackItem"):
            block = re.sub(r"<End>\d+</End>", f"<End>{start_ticks + duration_ticks}</End>", block)
            if start_ticks > 0:
                if "<Start>" in block:
                    block = re.sub(r"<Start>\d+</Start>", f"<Start>{start_ticks}</Start>", block)
                else:
                    block = re.sub(r"(<End>\d+</End>)", rf"\1\n\t\t\t\t\t<Start>{start_ticks}</Start>", block)
            else:
                block = re.sub(r"\s*<Start>\d+</Start>", "", block)
            block = re.sub(r"<ID>[0-9a-fA-F-]{36}</ID>", f"<ID>{new_item_guid}</ID>", block)
        elif block.startswith("<SubClip"):
            block = re.sub(r'<MasterClip ObjectURef="[0-9a-fA-F-]{36}"/>',
                            f'<MasterClip ObjectURef="{master_clip_uid}"/>', block)
            block = re.sub(r"<Name>[^<]*</Name>", f"<Name>{display_name}</Name>", block)
        elif block.startswith("<AudioClip"):
            block = re.sub(r'<Markers ObjectRef="\d+"/>', f'<Markers ObjectRef="{markers_id}"/>', block)
            block = re.sub(r'<Source ObjectRef="\d+"/>', f'<Source ObjectRef="{source_id}"/>', block)
            block = re.sub(r"<InPoint>\d+</InPoint>", "<InPoint>0</InPoint>", block)
            block = re.sub(r"<OutPoint>\d+</OutPoint>", f"<OutPoint>{duration_ticks}</OutPoint>", block)
            block = re.sub(r"<ClipID>[0-9a-fA-F-]{36}</ClipID>", f"<ClipID>{new_clip_id}</ClipID>", block)
        elif block.startswith("<SecondaryContent"):
            block = re.sub(r'<Content ObjectRef="\d+"/>', f'<Content ObjectRef="{source_id}"/>', block)
        patched.append(block)

    fragment = "\n".join(patched) + "\n"
    text = text.replace("</PremiereData>", fragment + "</PremiereData>")
    text = insert_track_item(text, "AudioClipTrack", track_uid, new_track_item_id)
    return text


def insert_track_item(text: str, track_tag: str, track_uid: str, item_object_id: str) -> str:
    """track_tag(VideoClipTrack/AudioClipTrack)のObjectUID=track_uidである
    トラックのClipItemsにTrackItemを1件追加する。トラックが空(TrackItems
    要素が無い)場合は新規に作る。"""
    span = find_block_by_object_uid(text, track_tag, track_uid)
    if span is None:
        raise ValueError(f"トラックが見つかりません: <{track_tag} ObjectUID=\"{track_uid}\">")
    block = text[span[0]:span[1]]

    if "<TrackItems" in block:
        n = len(re.findall(r"<TrackItem Index=", block))
        # 末尾(</TrackItems>の直前)に追加する: XML中の並び順とIndex値が
        # 一致していないとPremiereがクリップを正しく認識しない
        # (Index降順で先頭に挿入するバグにより実際に発生した不具合)。
        new_block = re.sub(
            r'(\s*)(</TrackItems>)',
            rf'\n\t\t\t\t\t<TrackItem Index="{n}" ObjectRef="{item_object_id}"/>\1\2',
            block, count=1,
        )
    else:
        new_block = re.sub(
            r'(<ClipItems Version="\d+">)',
            rf'\1\n\t\t\t\t<TrackItems Version="1">\n\t\t\t\t\t<TrackItem Index="0" ObjectRef="{item_object_id}"/>\n\t\t\t\t</TrackItems>',
            block, count=1,
        )
    return text[:span[0]] + new_block + text[span[1]:]


# ---------------------------------------------------------------------------
# ビデオトラックの新規追加 (V5, V6, ... のように末尾に追加していく)
# ---------------------------------------------------------------------------


def add_video_tracks(text: str, allocator: ObjectIdAllocator, count: int,
                      sequence_uid: str, donor_track_uid: str = None) -> tuple:
    """VideoTrackGroupの末尾に空のVideoClipTrackをcount個追加する。
    donor_track_uid省略時はテンプレの既存の空きトラックを自動で1つ探す。
    戻り値: (更新後のtext, 追加したTrackのObjectUIDリスト(Index昇順))"""
    group_id = _find_track_group_object_id(text, sequence_uid, "VideoTrackGroup")
    if donor_track_uid is None:
        donor_track_uid = find_empty_video_track_donor_uid(text, sequence_uid)
    group_span = find_block_by_object_id(text, "VideoTrackGroup", group_id)
    group_block = text[group_span[0]:group_span[1]]

    existing = re.findall(r'<Track Index="(\d+)" ObjectURef="[0-9a-fA-F-]{36}"/>', group_block)
    start_index = len(existing)
    next_id = int(re.search(r"<NextTrackID>(\d+)</NextTrackID>", group_block).group(1))

    donor_span = find_block_by_object_uid(text, "VideoClipTrack", donor_track_uid)
    donor_block = text[donor_span[0]:donor_span[1]]

    new_uids = []
    new_track_blocks = []
    new_track_entries = []
    for i in range(count):
        idx = start_index + i
        tid = next_id + i
        new_uid = new_guid()
        new_uids.append(new_uid)
        block = re.sub(r'ObjectUID="[0-9a-fA-F-]{36}"', f'ObjectUID="{new_uid}"', donor_block, count=1)
        block = re.sub(r"<ID>\d+</ID>", f"<ID>{tid}</ID>", block, count=1)
        block = re.sub(r"<Index>\d+</Index>", f"<Index>{idx}</Index>", block)
        new_track_blocks.append(block)
        new_track_entries.append(f'\t\t\t\t<Track Index="{idx}" ObjectURef="{new_uid}"/>')

    fragment = "\n".join(new_track_blocks) + "\n"
    text = text.replace("</PremiereData>", fragment + "</PremiereData>")

    new_group_block = re.sub(
        r"(</Tracks>)",
        "\n".join(new_track_entries) + r"\n\1",
        group_block, count=1,
    )
    new_group_block = re.sub(
        r"<NextTrackID>\d+</NextTrackID>",
        f"<NextTrackID>{next_id + count}</NextTrackID>",
        new_group_block,
    )
    text = text[:group_span[0]] + new_group_block + text[group_span[1]:]
    return text, new_uids


# ---------------------------------------------------------------------------
# SEクリップの配置 (既存ビン項目をそのまま流用、place_audio_on_trackの薄いラッパー)
# ---------------------------------------------------------------------------

def fix_media_file_path(text: str, media_uid: str) -> str:
    """テンプレのMediaオブジェクトのFilePathが、コピー元マシンの
    パス(存在しないフォルダ)を指したままになっているケースがある。
    ActualMediaFilePathが実在すればそちらをFilePathにも反映し、
    Premiereがオフラインメディアと誤認しないようにする。
    (このテンプレ固有の既知のデータ不備への対処。出力ファイル内でのみ
    書き換え、元テンプレは一切変更しない。)"""
    span = find_block_by_object_uid(text, "Media", media_uid)
    if span is None:
        return text
    block = text[span[0]:span[1]]
    actual = re.search(r"<ActualMediaFilePath>([^<]*)</ActualMediaFilePath>", block)
    filepath = re.search(r"<FilePath>([^<]*)</FilePath>", block)
    if not actual or not filepath:
        return text
    actual_path = actual.group(1)
    if filepath.group(1) == actual_path:
        return text
    if not os.path.exists(actual_path):
        return text
    new_block = block[:filepath.start(1)] + actual_path + block[filepath.end(1):]
    return text[:span[0]] + new_block + text[span[1]:]


def add_se_clip(text: str, allocator: ObjectIdAllocator, se_name: str,
                 start_ticks: int, track_uid: str) -> tuple:
    """テンプレのビンに既に存在するSE素材(se_name)を指定タイミングで
    トラックへ配置する。SE自体は新規インポート不要(既存MasterClipを
    そのまま参照する)。戻り値: (更新後のtext, end_ticks)"""
    donor = find_donor_masterclip(text, se_name)
    logging_block_span = find_block_by_object_id(text, "ClipLoggingInfo", donor["logging_info_id"])
    logging_block = text[logging_block_span[0]:logging_block_span[1]]
    duration_ticks = int(_tag_value(logging_block, "MediaOutPoint"))

    text = fix_media_file_path(text, donor["media_uid"])

    text = place_audio_on_track(
        text, allocator, track_uid,
        master_clip_uid=donor["master_clip_uid"],
        markers_id=donor["markers_id"],
        source_id=donor["source_id"],
        display_name=se_name,
        start_ticks=start_ticks,
        duration_ticks=duration_ticks,
        donor_master_clip_uid=donor["master_clip_uid"],
        donor_name=se_name,
    )
    return text, start_ticks + duration_ticks


# ---------------------------------------------------------------------------
# 静止画クリップの複製(RTFの緑ハイライト行に対応する画像を配置する)
# ---------------------------------------------------------------------------

def probe_image_size(path: str) -> tuple:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    )
    w, h = out.stdout.strip().split(",")
    return int(w), int(h)


def add_image_clip(text: str, allocator: ObjectIdAllocator, image_path: str,
                    start_ticks: int, end_ticks: int, track_uid: str,
                    color_label_index: int = None) -> str:
    """静止画ファイルをドナー一式(IMAGE_CLIP_DONOR_PATH、ユーザーの実プロジェクトから
    抽出した「インポート+配置済みの静止画クリップ」26オブジェクト)の複製で
    新規インポートし、指定トラックの指定タイミングへ配置する。

    yamaさんテンプレ.prproj自体には静止画クリップの実例が無いため、
    テロップ/SEのような同一ファイル内でのクローンではなく、別ファイルを
    ドナーとする(GraphClonerはsource_textとrenderされる文字列のみに依存し、
    allocatorは呼び出し側=対象ファイルのID空間で作るため、他ファイルを
    ドナーにしてもID衝突なく複製できる)。
    位置・スケール・回転・不透明度はドナー(等倍・中央配置)のまま変更しない。
    """
    with open(IMAGE_CLIP_DONOR_PATH, encoding="utf-8") as f:
        donor_text = f.read()

    abs_path = os.path.abspath(image_path)
    display_name = os.path.basename(image_path)
    width, height = probe_image_size(abs_path)
    duration_ticks = end_ticks - start_ticks

    cloner = GraphCloner(donor_text, allocator)
    cloner.plan("ClipProjectItem", "2f58fe41-ae42-40eb-8f36-af56909c9966", by_uid=True)
    cloner.plan("MasterClip", "38a137bd-bd82-4572-ac80-8e83dc83bc91", by_uid=True)
    cloner.plan("ClipLoggingInfo", "728")
    cloner.plan("VideoClip", "729")
    cloner.plan("Markers", "3118")
    cloner.plan("VideoMediaSource", "3119")
    cloner.plan("Media", "3739ed2d-473e-4f92-afd6-dfff0391c70d", by_uid=True)
    cloner.plan("VideoStream", "10069")
    cloner.plan("ClipChannelGroupVectorSerializer", "730")
    cloner.plan("SubClip", "28881")
    cloner.plan("VideoClip", "60069")
    cloner.plan("VideoComponentChain", "28880")
    cloner.plan("VideoFilterComponent", "60070")
    for pid in (168298, 168299, 168300, 168301, 168302, 168303,
                168304, 168305, 168306, 168307, 168308, 168309):
        tag = "PointComponentParam" if pid in (168298, 168299) else "VideoComponentParam"
        cloner.plan(tag, str(pid))
    cloner.plan("VideoClipTrackItem", "17170")

    blocks = cloner.render()
    # planした順は上のplan()呼び出し順と一致するため、インデックスで
    # マスター用VideoClip(729由来、blocks[3])とインスタンス用VideoClip
    # (60069由来、blocks[10])を確実に区別できる。
    master_idx, instance_idx = 3, 10
    assert blocks[master_idx].startswith("<VideoClip") and blocks[instance_idx].startswith("<VideoClip")

    new_master_clip_id = new_guid()
    new_instance_clip_id = new_guid()
    new_content_state = new_guid()
    new_file_key = new_guid()

    patched = []
    for i, block in enumerate(blocks):
        if block.startswith("<ClipProjectItem"):
            block = re.sub(r"<Name>[^<]*</Name>", f"<Name>{display_name}</Name>", block)
        elif block.startswith("<MasterClip"):
            block = re.sub(r"<Name>[^<]*</Name>", f"<Name>{display_name}</Name>", block)
        elif i == master_idx:
            # ドナー「マスター」レベルのVideoClip(729)。Premiereが静止画
            # インポート時に割り当てる既定のトリム窓をそのまま流用する
            # (実タイムライン尺は配置側=VideoClipTrackItemのStart/Endで決まる)。
            block = re.sub(r"<ClipID>[0-9a-fA-F-]{36}</ClipID>", f"<ClipID>{new_master_clip_id}</ClipID>", block)
        elif i == instance_idx:
            block = re.sub(r"<InPoint>\d+</InPoint>", "<InPoint>0</InPoint>", block)
            block = re.sub(r"<OutPoint>\d+</OutPoint>", f"<OutPoint>{duration_ticks}</OutPoint>", block)
            block = re.sub(r"<ClipID>[0-9a-fA-F-]{36}</ClipID>", f"<ClipID>{new_instance_clip_id}</ClipID>", block)
        elif block.startswith("<ClipLoggingInfo"):
            block = re.sub(r"<ClipName>[^<]*</ClipName>", f"<ClipName>{display_name}</ClipName>", block)
        elif block.startswith("<Media "):
            block = re.sub(r"<Title>[^<]*</Title>", f"<Title>{display_name}</Title>", block)
            block = re.sub(r"<FileKey>[0-9a-fA-F-]{36}</FileKey>", f"<FileKey>{new_file_key}</FileKey>", block)
            block = re.sub(r"<ContentAndMetadataState>[0-9a-fA-F-]{36}</ContentAndMetadataState>",
                            f"<ContentAndMetadataState>{new_content_state}</ContentAndMetadataState>", block)
            block = _patch_modification_state(block, new_content_state)
            block = re.sub(r"\s*<RelativePath>.*?</RelativePath>", "", block)
            block = re.sub(r"<ActualMediaFilePath>[^<]*</ActualMediaFilePath>",
                            f"<ActualMediaFilePath>{abs_path}</ActualMediaFilePath>", block)
            block = re.sub(r"<FilePath>[^<]*</FilePath>", f"<FilePath>{abs_path}</FilePath>", block)
        elif block.startswith("<VideoStream"):
            block = re.sub(r"<FrameRect>[\d,]+</FrameRect>", f"<FrameRect>0,0,{width},{height}</FrameRect>", block)
        elif block.startswith("<SubClip"):
            block = re.sub(r'<MasterClip ObjectURef="[0-9a-fA-F-]{36}"/>',
                            f'<MasterClip ObjectURef="{cloner.guid_map["38a137bd-bd82-4572-ac80-8e83dc83bc91"]}"/>', block)
            block = re.sub(r"<Name>[^<]*</Name>", f"<Name>{display_name}</Name>", block)
        elif block.startswith("<VideoComponentChain"):
            block = re.sub(
                r"<Components Version=\"1\">.*?</Components>",
                f'<Components Version="1">\n\t\t\t\t<Component Index="0" ObjectRef="{cloner.id_map["60070"]}"/>\n\t\t\t</Components>',
                block, flags=re.S,
            )
        elif block.startswith("<VideoClipTrackItem"):
            block = re.sub(r"<End>\d+</End>", f"<End>{end_ticks}</End>", block)
            if start_ticks > 0:
                if "<Start>" in block:
                    block = re.sub(r"<Start>\d+</Start>", f"<Start>{start_ticks}</Start>", block)
                else:
                    block = re.sub(r"(<End>\d+</End>)", rf"\1\n\t\t\t\t\t<Start>{start_ticks}</Start>", block)
            else:
                block = re.sub(r"\s*<Start>\d+</Start>", "", block)
        patched.append(block)

    if color_label_index is not None:
        new_label = f"BE.Prefs.LabelColors.{color_label_index}"
        master_block = patched[master_idx]
        if "<asl.clip.label.name>" in master_block:
            master_block = re.sub(r"<asl\.clip\.label\.name>[^<]*</asl\.clip\.label\.name>",
                                    f"<asl.clip.label.name>{new_label}</asl.clip.label.name>", master_block)
        else:
            master_block = re.sub(r"(<Properties Version=\"1\">)",
                                    rf"\1\n\t\t\t\t\t<asl.clip.label.name>{new_label}</asl.clip.label.name>",
                                    master_block, count=1)
        patched[master_idx] = master_block

    fragment = "\n".join(patched) + "\n"
    text = text.replace("</PremiereData>", fragment + "</PremiereData>")

    new_bin_item_uid = cloner.guid_map["2f58fe41-ae42-40eb-8f36-af56909c9966"]
    text = re.sub(
        r'(<Item Index="\d+" ObjectURef="[0-9a-fA-F-]{36}"/>\s*)(</Items>\s*</ProjectItemContainer>\s*</RootProjectItem>)',
        lambda m: m.group(1) + _new_bin_item_line(text, new_bin_item_uid) + m.group(2),
        text, count=1,
    )

    new_track_item_id = cloner.id_map["17170"]
    text = insert_track_item(text, "VideoClipTrack", track_uid, new_track_item_id)
    return text


# ---------------------------------------------------------------------------
# テロップ(スタイルクリップ)の複製
# ---------------------------------------------------------------------------

def _find_tag_for_id(text: str, object_id: str) -> str:
    m = re.search(rf'<(\w+) ObjectID="{object_id}"', text)
    if not m:
        raise ValueError(f"ObjectID={object_id} を持つ要素が見つかりません")
    return m.group(1)


def get_style_master_text_blob(text: str, style_uid: str) -> bytes:
    """StyleProjectItem(スタイルカタログの「マスター」定義そのもの、
    タイムライン配置とは無関係)から、そのスタイル自身のソーステキスト
    ArbVideoComponentParamの生バイナリを取得する。フォント名・サイズ・
    塗り・背景等の見た目がここに焼き込まれている。"""
    span = find_block_by_object_uid(text, "StyleProjectItem", style_uid)
    if span is None:
        raise ValueError(f"StyleProjectItemが見つかりません: {style_uid}")
    block = text[span[0]:span[1]]
    comp_id = re.search(r'<Component ObjectRef="(\d+)"/>', block).group(1)
    comp_span = find_block_by_object_id(text, "VideoFilterComponent", comp_id)
    comp_block = text[comp_span[0]:comp_span[1]]
    param0_id = re.search(r'<Param Index="0" ObjectRef="(\d+)"/>', comp_block).group(1)
    param_span = find_block_by_object_id(text, "ArbVideoComponentParam", param0_id)
    param_block = text[param_span[0]:param_span[1]]
    m = re.search(r'<StartKeyframeValue Encoding="base64" BinaryHash="[^"]*">([^<]*)</StartKeyframeValue>', param_block, re.S)
    return base64.b64decode(m.group(1).strip())


def find_donor_telop_by_style(text: str, style_uid: str) -> dict:
    """指定スタイルUIDを使っているスタイルカタログのテロップクリップ一式を
    ドナーとして探す。"""
    text_filter_id = None
    for m in re.finditer(r'<VideoFilterComponent ObjectID="(\d+)"[^>]*>.*?</VideoFilterComponent>', text, re.S):
        block = m.group(0)
        if "<MatchName>AE.ADBE Text</MatchName>" not in block:
            continue
        if f'<ParentStyle ObjectURef="{style_uid}"/>' in block:
            text_filter_id = m.group(1)
            break
    if text_filter_id is None:
        raise ValueError(f"スタイルUID {style_uid} を使うテロップが見つかりません")

    # このフィルタを含むVideoComponentChainとその全コンポーネントを探す。
    # AE.ADBE Text以外のフィルタ(inスライド緩急などのGeometry2系
    # エントランスアニメーション)は、ユーザー指示によりアニメーション無し
    # (静止表示)にするため、そもそもクローン対象から除外する。
    chain_id = None
    component_ids = []
    for m in re.finditer(r'<VideoComponentChain ObjectID="(\d+)"[^>]*>.*?</VideoComponentChain>', text, re.S):
        block = m.group(0)
        if f'ObjectRef="{text_filter_id}"' in block:
            chain_id = m.group(1)
            all_component_ids = re.findall(r'<Component Index="\d+" ObjectRef="(\d+)"/>', block)
            for cid in all_component_ids:
                cid_span = find_block_by_object_id(text, "VideoFilterComponent", cid)
                cid_block = text[cid_span[0]:cid_span[1]] if cid_span else ""
                if "<MatchName>AE.ADBE Text</MatchName>" in cid_block:
                    component_ids.append(cid)
            break
    if chain_id is None:
        raise ValueError("VideoComponentChainが見つかりません")

    # このチェーンを使っているVideoClipTrackItemとSubClipを探す
    track_item_id = None
    subclip_id = None
    for m in re.finditer(r'<VideoClipTrackItem ObjectID="(\d+)"[^>]*>.*?</VideoClipTrackItem>', text, re.S):
        block = m.group(0)
        if f'<Components ObjectRef="{chain_id}"/>' in block:
            track_item_id = m.group(1)
            subclip_id = re.search(r'<SubClip ObjectRef="(\d+)"/>', block).group(1)
            break
    if track_item_id is None:
        raise ValueError("VideoClipTrackItemが見つかりません")

    subclip_block_span = find_block_by_object_id(text, "SubClip", subclip_id)
    subclip_block = text[subclip_block_span[0]:subclip_block_span[1]]
    video_clip_id = _tag_ref(subclip_block, "Clip")
    graphic_master_clip_uid = _tag_ref(subclip_block, "MasterClip")

    # 各コンポーネント(フィルタ)のParamsを列挙
    filter_params = {}  # component_id -> [param_id, ...]
    for cid in component_ids:
        block_span = find_block_by_object_id(text, "VideoFilterComponent", cid)
        block = text[block_span[0]:block_span[1]]
        param_ids = re.findall(r'<Param Index="\d+" ObjectRef="(\d+)"/>', block)
        filter_params[cid] = param_ids

    return dict(
        text_filter_id=text_filter_id,
        chain_id=chain_id,
        component_ids=component_ids,
        filter_params=filter_params,
        track_item_id=track_item_id,
        subclip_id=subclip_id,
        video_clip_id=video_clip_id,
        graphic_master_clip_uid=graphic_master_clip_uid,
    )


# カタログ内に実際の配置例(ドナー)が無い/構造が複雑すぎるスタイル
# (例: yamaさん基本、テロップベースを含むセリフ系)向けのフォールバック。
# 「強調」は単純な構造(Geometry2+Text)で全テストで動作実績があるため、
# 構造だけこれを借りてParentStyleだけ目的のスタイルに差し替える。
FALLBACK_STRUCTURAL_STYLE_UID = "20ee147b-7e96-45a5-a9ec-5b9dbe45eb2c"  # 強調


def add_telop_clip(text: str, allocator: ObjectIdAllocator, style_uid: str,
                    display_text: str, start_ticks: int, end_ticks: int,
                    track_uid: str, color_label_index: int = None,
                    target_scale: float = None) -> str:
    """スタイルカタログのテロップクリップ一式を複製し、テキストと
    タイミングを差し替えて指定トラックへ追加する。

    style_uidに対応するドナーがカタログに無い、または構造が複雑
    (VideoClipComponent等を含む)場合はFALLBACK_STRUCTURAL_STYLE_UIDの
    構造を借り、ParentStyleだけstyle_uidに差し替える。
    color_label_indexを指定すると複製したVideoClipのクリップカラー
    ラベル(asl.clip.label.name = BE.Prefs.LabelColors.N)を上書きする。
    target_scaleを指定すると、ドナーのスケール値を問答無用でこの値に
    置き換える(カタログ内の一部スタイルが見本用に100%を大きく超える
    拡大率になっており、実際の長いテキストだとセーフマージンを
    はみ出すため)。
    位置(Position)・アンカーポイント・回転・不透明度はここでは一切
    変更しない(ドナーの値をそのまま使う)。
    """
    override_style_uid = None
    try:
        donor = find_donor_telop_by_style(text, style_uid)
    except Exception:
        donor = find_donor_telop_by_style(text, FALLBACK_STRUCTURAL_STYLE_UID)
        override_style_uid = style_uid

    cloner = GraphCloner(text, allocator)
    cloner.plan("VideoClipTrackItem", donor["track_item_id"])
    cloner.plan("VideoComponentChain", donor["chain_id"])
    for cid in donor["component_ids"]:
        tag = _find_tag_for_id(text, cid)
        cloner.plan(tag, cid)
        for pid in donor["filter_params"][cid]:
            ptag = _find_tag_for_id(text, pid)
            cloner.plan(ptag, pid)
    cloner.plan("SubClip", donor["subclip_id"])
    cloner.plan(_find_tag_for_id(text, donor["video_clip_id"]), donor["video_clip_id"])

    blocks = cloner.render()

    new_clip_id = new_guid()

    text_filter_new_id = cloner.id_map[donor["text_filter_id"]]

    patched = []
    for block in blocks:
        if block.startswith("<VideoComponentChain"):
            # component_ids は find_donor_telop_by_style 側で既にAE.ADBE Text
            # のみに絞ってあるため、Componentsリストも1件(Index=0のText)だけに
            # 書き換える(Geometry2等のアニメーション用フィルタ参照を残さない)。
            block = re.sub(
                r"<Components Version=\"1\">.*?</Components>",
                f'<Components Version="1">\n\t\t\t\t<Component Index="0" ObjectRef="{text_filter_new_id}"/>\n\t\t\t</Components>',
                block, flags=re.S,
            )
        if block.startswith("<VideoClipTrackItem"):
            block = re.sub(r"<End>\d+</End>", f"<End>{end_ticks}</End>", block)
            if start_ticks > 0:
                if "<Start>" in block:
                    block = re.sub(r"<Start>\d+</Start>", f"<Start>{start_ticks}</Start>", block)
                else:
                    block = re.sub(r"(<End>\d+</End>)", rf"\1\n\t\t\t\t\t<Start>{start_ticks}</Start>", block)
            else:
                block = re.sub(r"\s*<Start>\d+</Start>", "", block)
        elif block.startswith("<SubClip"):
            block = re.sub(r'<MasterClip ObjectURef="[0-9a-fA-F-]{36}"/>',
                            f'<MasterClip ObjectURef="{donor["graphic_master_clip_uid"]}"/>', block)
        elif block.startswith("<VideoClip "):
            block = re.sub(r"<ClipID>[0-9a-fA-F-]{36}</ClipID>", f"<ClipID>{new_clip_id}</ClipID>", block)
            if color_label_index is not None:
                new_label = f"BE.Prefs.LabelColors.{color_label_index}"
                if "<asl.clip.label.name>" in block:
                    block = re.sub(r"<asl\.clip\.label\.name>[^<]*</asl\.clip\.label\.name>",
                                    f"<asl.clip.label.name>{new_label}</asl.clip.label.name>", block)
                else:
                    block = re.sub(r"(<Properties Version=\"1\">)",
                                    rf"\1\n\t\t\t\t\t<asl.clip.label.name>{new_label}</asl.clip.label.name>",
                                    block, count=1)
        if override_style_uid and "<ParentStyle ObjectURef=" in block:
            block = re.sub(r'<ParentStyle ObjectURef="[0-9a-fA-F-]{36}"/>',
                            f'<ParentStyle ObjectURef="{override_style_uid}"/>', block)
        patched.append(block)

    # スケール(Index3のVideoComponentParam「スケール」)をtarget_scaleに固定する
    if target_scale is not None:
        text_filter_params = donor["filter_params"][donor["text_filter_id"]]
        if len(text_filter_params) > 3:
            scale_new_id = cloner.id_map.get(text_filter_params[3])
            if scale_new_id:
                for i, block in enumerate(patched):
                    if block.startswith(f'<VideoComponentParam ObjectID="{scale_new_id}"'):
                        m = re.search(r"(<StartKeyframe>-?\d+,)([\d.]+),", block)
                        if m:
                            # 実データのスケール値は "212." のように整数+ドットの形式
                            # だったため、それに合わせて整数に丸めて出力する
                            # (小数のまま出力すると "138.6." のように不正な
                            # 二重ドットになるバグがあったため)。
                            patched[i] = block[:m.start(2)] + f"{round(target_scale)}." + block[m.end(2):]
                        break

        # 位置(Index2のPointComponentParam「位置」)を960,1034(正規化0.5,
        # 0.95740741491317749)に、アンカーポイント(Index8のPointComponentParam)
        # を0,0に、それぞれ強制する。カタログ内のドナースタイルによって
        # 素の配置位置・アンカーポイントがまちまち(例:「ポジティブ」スタイルは
        # Position=0.125:0.65、アンカーポイント=-0.375:-0.3)なため、
        # スケールだけでなく位置・アンカーポイントも明示的に上書きしないと
        # セーフマージン外のスタイルが混入してしまう。ユーザー指示通り
        # Positionとアンカーポイントはそれぞれこの1点(960,1034 / 0,0)に固定し、
        # それ以外(回転・不透明度等)はドナーの値のまま一切変更しない。
        for param_index, fixed_value in ((2, "0.5:0.95740741491317749"), (8, "0:0")):
            if len(text_filter_params) <= param_index:
                continue
            target_new_id = cloner.id_map.get(text_filter_params[param_index])
            if not target_new_id:
                continue
            for i, block in enumerate(patched):
                if block.startswith(f'<PointComponentParam ObjectID="{target_new_id}"'):
                    m = re.search(r"(<StartKeyframe>-?\d+,)-?[\d.]+:-?[\d.]+(,)", block)
                    if m:
                        patched[i] = (block[:m.start()] + m.group(1)
                                      + fixed_value + m.group(2)
                                      + block[m.end():])
                    break

    # ソーステキストを差し替える(ArbVideoComponentParam, ParameterID=1 = Index0)。
    # このブロブはテキスト本体だけでなく、フォント名・サイズ・塗り・背景等の
    # 見た目情報も埋め込まれた「そのインスタンス自身の焼き込み済みスタイル」
    # であることが判明している(実データ調査で、例えば「強調」ドナーの
    # ブロブにはSourceHanSans-Heavy、「yamaさん基本」自身のブロブには
    # HiraMinProN-W6が埋め込まれていた)。override_style_uid指定時
    # (=構造だけ他スタイルを借りるフォールバック経路)は、ParentStyle参照を
    # 差し替えるだけでなく、ブロブ自体も目的のスタイル自身のブロブを
    # ベースにする(構造ドナーのブロブをそのまま使うと、ParentStyleの
    # 参照先とは無関係に構造ドナー自身の見た目が焼き込まれたままになる
    # ため)。
    source_param_old_id = donor["filter_params"][donor["text_filter_id"]][0]
    if override_style_uid:
        base_data = get_style_master_text_blob(text, override_style_uid)
    else:
        source_block_span = find_block_by_object_id(text, "ArbVideoComponentParam", source_param_old_id)
        source_block = text[source_block_span[0]:source_block_span[1]]
        m0 = re.search(r'<StartKeyframeValue Encoding="base64" BinaryHash="[^"]*">([^<]*)</StartKeyframeValue>', source_block, re.S)
        base_data = base64.b64decode(m0.group(1).strip())

    source_param_new_id = cloner.id_map[source_param_old_id]
    for i, block in enumerate(patched):
        if block.startswith(f'<ArbVideoComponentParam ObjectID="{source_param_new_id}"'):
            m = re.search(r'(<StartKeyframeValue Encoding="base64" BinaryHash="[^"]*">)([^<]*)(\s*</StartKeyframeValue>)', block, re.S)
            new_data = replace_source_text(base_data, display_text)
            new_b64 = base64.b64encode(new_data).decode("ascii")
            block = block[:m.start()] + m.group(1) + new_b64 + m.group(3) + block[m.end():]
            patched[i] = block
            break

    fragment = "\n".join(patched) + "\n"
    text = text.replace("</PremiereData>", fragment + "</PremiereData>")

    new_track_item_id = cloner.id_map[donor["track_item_id"]]
    text = insert_track_item(text, "VideoClipTrack", track_uid, new_track_item_id)
    return text


def _new_bin_item_line(text: str, new_uid: str) -> str:
    # Root Binの既存Item数からIndexを決める
    m = re.search(r'<ProjectItemContainer Version="1">\s*<Items Version="1">(.*?)</Items>', text, re.S)
    n = len(re.findall(r"<Item Index=", m.group(1)))
    return f'\t\t\t\t<Item Index="{n}" ObjectURef="{new_uid}"/>\n'


def _patch_modification_state(block: str, guid: str) -> str:
    """ModificationStateの中身はGUID文字列をUTF-16LEでbase64化したものと
    判明している(donorのContentAndMetadataStateと完全一致することを確認済み)。
    BinaryHash属性自体はレンダーキャッシュ用の値と推測され、意味は不明の
    ため変更せず据え置く。"""
    import base64
    b64 = base64.b64encode(guid.encode("utf-16-le")).decode("ascii")

    def _sub(m):
        return f"{m.group(1)}{b64}{m.group(2)}"

    return re.sub(
        r'(<ModificationState Encoding="base64" BinaryHash="[^"]*">)[A-Za-z0-9+/=]*(\s*</ModificationState>)',
        _sub, block, flags=re.S,
    )


def load_style_category_map(style_config_path: str) -> dict:
    """style_config.json から style_name -> カテゴリ名 の辞書を作る。
    style_config_pathがNone(未指定)ならカテゴリ分類なしとして空辞書を返す
    (この場合、強調フレーズは全てSCALE_CAPPED_CATEGORIES対象外・固定SE
    ペアリングも行われない)。"""
    if not style_config_path:
        return {}
    import json
    with open(style_config_path, encoding="utf-8") as f:
        d = json.load(f)
    m = {}
    for cat_name, cat in d.get("categories", {}).items():
        for style_name in cat.get("styles", {}):
            m[style_name] = cat_name
    return m


def load_style_config(style_config_path: str, style_map: dict) -> None:
    """style_config.json(analyze-templateで作るstyle_jsonとは別の、
    テンプレのスタイルカタログをどう使うかを決める設定ファイル)を
    読み込み、モジュールレベルの各種設定を上書きする。
    style_config_pathがNoneの場合は既定値(全て空/未設定)のままにする
    (=基本テロップ用スタイル名が特定できないため、その場合base_captions
    の生成はできない)。"""
    global BASE_CAPTION_STYLE_NAME, BASE_CAPTION_COLOR_LABEL, FALLBACK_STRUCTURAL_STYLE_UID
    global CATEGORY_COLOR_LABEL, STYLE_FONT_NAME, FONT_PATHS, SCALE_CAPPED_CATEGORIES
    global _CALIBRATION_TEXT, _CALIBRATION_SCALE_PCT, _CALIBRATION_FONT, _SAFE_WIDTH_UNITS

    if not style_config_path:
        return
    import json
    with open(style_config_path, encoding="utf-8") as f:
        cfg = json.load(f)

    if cfg.get("base_caption_style_name"):
        BASE_CAPTION_STYLE_NAME = cfg["base_caption_style_name"]
    base_color = cfg.get("base_caption_color_label")
    if base_color in LABEL_COLOR_INDEX:
        BASE_CAPTION_COLOR_LABEL = LABEL_COLOR_INDEX[base_color]

    fallback_name = cfg.get("fallback_structural_style_uid_style_name")
    if fallback_name and fallback_name in style_map:
        FALLBACK_STRUCTURAL_STYLE_UID = style_map[fallback_name]

    cat_colors = cfg.get("category_color_label", {})
    CATEGORY_COLOR_LABEL = {
        cat: LABEL_COLOR_INDEX[name] for cat, name in cat_colors.items() if name in LABEL_COLOR_INDEX
    }

    SCALE_CAPPED_CATEGORIES = set(cfg.get("scale_capped_categories", []))

    font_paths_cfg = cfg.get("font_paths", {})
    resolved = dict(BUNDLED_FONT_PATHS)
    for name, p in font_paths_cfg.items():
        resolved[name] = BUNDLED_FONT_PATHS.get(name, os.path.join(ASSETS_DIR, "SourceHanSerif-Heavy.otf")) if p == "@bundled" else p
    FONT_PATHS = {}
    for name, p in resolved.items():
        if os.path.exists(p):
            FONT_PATHS[name] = p
        else:
            print(f"  [警告] フォントファイルが見つかりません: {name} ({p})。"
                  f"該当スタイルはスケール自動調整をスキップします。", file=sys.stderr)

    style_font_cfg = cfg.get("style_font_name", {})
    STYLE_FONT_NAME = {k: v for k, v in style_font_cfg.items() if v in FONT_PATHS}

    calib = cfg.get("scale_calibration", {})
    if calib.get("font") in FONT_PATHS and calib.get("text") and calib.get("scale_pct"):
        _CALIBRATION_TEXT = calib["text"]
        _CALIBRATION_SCALE_PCT = float(calib["scale_pct"])
        _CALIBRATION_FONT = calib["font"]
        _SAFE_WIDTH_UNITS = _measure_text_width(_CALIBRATION_TEXT, _CALIBRATION_FONT) * (_CALIBRATION_SCALE_PCT / 100.0)
    else:
        print("  [警告] スケールキャリブレーション用フォントが見つからないため、"
              "ポジティブ/ネガティブのスケール自動調整は無効になります"
              "(ドナーの値をそのまま使います)。", file=sys.stderr)
        _SAFE_WIDTH_UNITS = None


# ポジティブ/ネガティブのスケール自動計算。
# 位置(Position)・アンカーポイント・回転・不透明度は一切変更しない
# (ドナーの値をそのまま使う)。スケールのみ、実際のフォント・文字幅を
# 実測してセーフマージンの内側ぎりぎりに収まるよう計算する
# (文字数ベースの反比例モデルは、スタイルごとにフォントが異なる場合に
# 実描画幅を近似できず破綻することが実データで確認されたため不採用)。
#
# フォント名は各スタイルのAE.ADBE Textコンポーネント(Index=0
# 「ソーステキスト」のバイナリ内にプレーンASCIIで埋め込まれている)から
# 実際に使用フォントを特定できる。フォントファイルの実体は環境依存
# (Adobe Creative Cloudのローカルキャッシュ等)なので、テンプレごとの
# style_config.json(font_paths)で指定する。指定が無い/ファイルが
# 見つからないスタイルは、スケール自動調整をスキップし、テンプレの
# ドナー値をそのまま使う(load_style_config()参照)。
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
BUNDLED_FONT_PATHS = {
    "SourceHanSerif-Heavy": os.path.join(ASSETS_DIR, "SourceHanSerif-Heavy.otf"),
}
FONT_PATHS = dict(BUNDLED_FONT_PATHS)
STYLE_FONT_NAME: dict = {}

# キャリブレーション: ユーザーがPremiereで実測した「セーフマージンの
# 内側ぎりぎりに収まる」実例(煽り_8スタイル、SourceHanSerif-Heavy)。
#   テキスト "もうさ 頑張るのやめませんか?" は
#   Scale=100%だとNG(セーフマージンをはみ出す)、Scale=139%でOK。
# PILでの実測幅(フォントサイズ100基準) = 1374.0
# → SAFE_WIDTH_UNITS(フォントサイズ100基準での「ぴったり収まる幅」) =
#   1374.0 * 1.39 = 1909.86
# 他のスタイルのフォントもすべて同じ基準サイズ100で実測し、この1定数に
# 対する比率でスケールを求める(カタログ内の全スタイルが同じ土台サイズで
# 作られている前提。Premiereでの目視確認を推奨する)。
SCALE_CAPPED_CATEGORIES = {"ポジティブ", "ネガティブ"}
_CALIBRATION_TEXT = "もうさ 頑張るのやめませんか?"
_CALIBRATION_SCALE_PCT = 139.0
_CALIBRATION_FONT = "SourceHanSerif-Heavy"
SCALE_MIN = 50.0
SCALE_MAX = 300.0
_SAFE_WIDTH_UNITS = None  # load_style_config()で計算済みなら値が入る

_font_cache: dict = {}


def _get_font(font_name: str, size: int = 100):
    key = (font_name, size)
    if key not in _font_cache:
        from PIL import ImageFont
        path = FONT_PATHS[font_name]
        _font_cache[key] = ImageFont.truetype(path, size)
    return _font_cache[key]


def _measure_text_width(text: str, font_name: str) -> float:
    return _get_font(font_name).getlength(text)


def compute_scale_for_text(text: str, style_name: str):
    """実フォントでの文字幅計測に基づき、セーフマージンぎりぎりに
    収まるスケール(%)を返す。該当スタイルのフォントが未設定/未検出
    (load_style_config()でスキップされた場合を含む)ならNoneを返し、
    呼び出し側はスケールを上書きしない(ドナーの値のまま)。"""
    if _SAFE_WIDTH_UNITS is None:
        return None
    font_name = STYLE_FONT_NAME.get(style_name)
    if font_name is None or font_name not in FONT_PATHS:
        return None
    width = _measure_text_width(text, font_name)
    if width <= 0:
        return SCALE_MIN
    scale = _SAFE_WIDTH_UNITS / width * 100.0
    return min(max(scale, SCALE_MIN), SCALE_MAX)


def _text_segments_excluding_phrases(cue_text: str, phrases_in_order: list) -> list:
    """cue_text中でphrases_in_order(出現順)を取り除いた残りのテキスト
    断片をリストで返す(空文字列は含めない)。_subtract_intervalsの
    時間軸バージョンに対応する、文字位置ベースの分割。
    強調テロップで抜いた区間には基本テロップの文字も出さないようにする
    ために使う(そうしないと分割後の全断片にキュー全文をそのまま
    表示することになり、同じ文が何度もフラッシュして見える)。"""
    segments = []
    cur = 0
    for phrase in phrases_in_order:
        idx = cue_text.find(phrase, cur)
        if idx < 0:
            idx = cue_text.find(phrase)
        if idx < 0:
            continue
        if idx > cur:
            segments.append(cue_text[cur:idx])
        cur = max(cur, idx + len(phrase))
    if cur < len(cue_text):
        segments.append(cue_text[cur:])
    return [s for s in segments if s.strip()]


def _subtract_intervals(start: int, end: int, exclude: list) -> list:
    """[start, end) から exclude(ticksの(s, e)ペアのリスト、時系列順)を
    取り除いた残り区間のリストを返す。"""
    segments = []
    cur = start
    for s, e in exclude:
        if s > cur:
            segments.append((cur, s))
        cur = max(cur, e)
    if cur < end:
        segments.append((cur, end))
    return segments


def apply_align_json(text: str, allocator: ObjectIdAllocator, align_json_path: str,
                      style_json_path: str, style_se_categories_path: str,
                      telop_track_uid: str, base_track_uid: str, se_track_uid: str,
                      image_track_uid: str = None, image_map: dict = None,
                      se_track_uid2: str = None) -> str:
    """align_script.pyの出力を読み込み、
    - 強調テロップ/ポジティブ/ネガティブを telop_track_uid に(カテゴリ別に
      色ラベルを付け、ポジティブ/ネガティブは文字数に応じたスケールで
      セーフマージンぎりぎりに収まるよう調整して。位置は変更しない)
    - base_captions(無音区間+文字数上限で分割済みの逐語キャプション)を
      base_track_uid に(強調テロップ/ポジティブ/ネガティブが表示される
      時間帯はキャプション側を分割して非表示にする)
    - SEをse_track_uidに(se_track_uid2が渡されていれば、1本目のトラック
      で時間が重なる場合に2本目へ振り分けることで、SE自体の再生時間が
      詰まっている場合の取りこぼしを減らす)
    まとめて差し込む。同一トラック上で時間が重なる項目は構造が壊れる
    ため、重なった項目はスキップして警告を出す(トラック追加による
    自動回避はしない)。"""
    import json

    with open(align_json_path, encoding="utf-8") as f:
        align = json.load(f)
    style_map = load_style_uid_map(style_json_path)
    load_style_config(style_se_categories_path, style_map)
    category_map = load_style_category_map(style_se_categories_path)
    base_style_uid = style_map.get(BASE_CAPTION_STYLE_NAME)
    if not base_style_uid:
        raise ValueError(f"基本テロップ用スタイルが見つかりません: {BASE_CAPTION_STYLE_NAME}")

    telop_cursor_end = 0
    se_cursor_end = 0
    se_cursor_end2 = 0
    base_cursor_end = 0
    n_telop = n_se = n_base = n_skip_telop = n_skip_se = n_skip_style = 0

    # --- 強調テロップ/SE: 全キューの有効な強調エントリをまず時系列で集める ---
    valid_emphasis = []  # (e_start, e_end, phrase, style_name, se_file)
    for cue in align.get("cues", []):
        for emph in cue.get("emphasis", []):
            phrase = emph.get("phrase")
            style_name = emph.get("style_name")
            e_start = sec_to_ticks(emph["start"])
            e_end = sec_to_ticks(emph["end"])
            if not phrase or e_start >= e_end:
                continue
            if style_name not in style_map:
                print(f"  [skip] 未知のスタイル名: {style_name!r} (text={phrase!r})", file=sys.stderr)
                n_skip_style += 1
                continue
            valid_emphasis.append((e_start, e_end, phrase, style_name, emph.get("se_file")))
    valid_emphasis.sort(key=lambda x: x[0])
    emphasis_intervals = [(s, e) for s, e, *_ in valid_emphasis]

    # --- 基本テロップ: align_script.py が無音区間+文字数上限で分割済みの
    # base_captions をそのまま使う。強調テロップと重なる時間帯だけ、
    # このセグメント単位でさらに文字位置ベースで分割して除外する。
    for seg in align.get("base_captions", []):
        seg_start = sec_to_ticks(seg["start"])
        seg_end = sec_to_ticks(seg["end"])
        seg_text = seg.get("text", "")
        if not seg_text or seg_start >= seg_end:
            continue
        overlapping = [(s, e) for s, e in emphasis_intervals if s < seg_end and e > seg_start]
        # このセグメントと時間的に重なる強調フレーズのうち、実際にこの
        # セグメントのテキストに含まれるものだけに絞る。RTFモードでは
        # 基本テロップと強調テロップはRTFの行単位で完全に別カテゴリ
        # (排他)であり、基本テロップ側のテキストに強調フレーズがそのまま
        # 含まれることは無い。そのため、V8の隙間埋め(前回追加)で基本
        # テロップの終了時刻が延び、たまたま時間的にはV9強調テロップの
        # 区間と重なることがあっても、テキスト上の重複が無ければ分割は
        # 行わない(分割すると、テキスト側は1個のままなのに時間側だけ
        # 2〜3個に別れ、zip()で後半の時間区間が無言で捨てられ、隙間埋めが
        # 台無しになるバグがあったため)。
        phrases_here = []
        for s, e, phrase, *_ in valid_emphasis:
            if s < seg_end and e > seg_start and phrase in seg_text:
                phrases_here.append(phrase)
        if not phrases_here:
            time_segments = [(seg_start, seg_end)]
            text_segments = [seg_text]
        else:
            clipped_overlap = [(max(s, seg_start), min(e, seg_end)) for s, e in overlapping]
            time_segments = _subtract_intervals(seg_start, seg_end, clipped_overlap)
            text_segments = _text_segments_excluding_phrases(seg_text, phrases_here)
            if len(time_segments) != len(text_segments):
                print(f"  [警告] 基本テロップ区間 {seg['start']}s-{seg['end']}s: "
                      f"時間区間数({len(time_segments)})とテキスト区間数({len(text_segments)})が"
                      f"一致しません。対応できた分のみ使用します。", file=sys.stderr)

        for (s, e), t in zip(time_segments, text_segments):
            if s < base_cursor_end:
                s = base_cursor_end
            if s >= e:
                continue
            if not t.strip(" 　、。！？!?,.・…\n"):
                continue
            text = add_telop_clip(text, allocator, base_style_uid, t, s, e, base_track_uid,
                                   color_label_index=BASE_CAPTION_COLOR_LABEL)
            base_cursor_end = e
            n_base += 1

    # --- 強調テロップ本体+SE ---
    for e_start, e_end, phrase, style_name, se_file in valid_emphasis:
        style_uid = style_map[style_name]
        if e_start < telop_cursor_end:
            print(f"  [skip] テロップの時間帯が前の項目と重複: {phrase!r} "
                  f"({e_start / SECONDS_PER_TICK:.3f}s)", file=sys.stderr)
            n_skip_telop += 1
        else:
            category = category_map.get(style_name)
            color = CATEGORY_COLOR_LABEL.get(category)
            is_capped = category in SCALE_CAPPED_CATEGORIES
            target_scale = compute_scale_for_text(phrase, style_name) if is_capped else None
            text = add_telop_clip(text, allocator, style_uid, phrase, e_start, e_end,
                                   telop_track_uid, color_label_index=color,
                                   target_scale=target_scale)
            telop_cursor_end = e_end
            n_telop += 1

        if se_file:
            if e_start >= se_cursor_end:
                text, se_cursor_end = add_se_clip(text, allocator, se_file, e_start, se_track_uid)
                n_se += 1
            elif se_track_uid2 and e_start >= se_cursor_end2:
                text, se_cursor_end2 = add_se_clip(text, allocator, se_file, e_start, se_track_uid2)
                n_se += 1
            else:
                print(f"  [skip] SEの時間帯が前の項目と重複: {se_file!r} "
                      f"({e_start / SECONDS_PER_TICK:.3f}s)", file=sys.stderr)
                n_skip_se += 1

    # --- 画像(RTFの緑ハイライト行に対応する静止画): image_track_uidへ、
    # 各行のクリップと同じ開始・終了タイミングで配置する。image_mapは
    # align.jsonのimage_cuesのindexキーに対応するローカル画像ファイル
    # パスの辞書(main()側で事前に用意する)。対応する画像が無い行は
    # スキップする(画像の自動検索・ダウンロードはこの関数の責務外)。
    n_image = n_skip_image = 0
    if image_track_uid and image_map:
        image_cursor_end = 0
        for cue in align.get("image_cues", []):
            path = image_map.get(cue["index"])
            if not path:
                print(f"  [skip] 画像が用意されていません: index={cue['index']} text={cue.get('text')!r}",
                      file=sys.stderr)
                n_skip_image += 1
                continue
            i_start = sec_to_ticks(cue["start"])
            i_end = sec_to_ticks(cue["end"])
            if i_start >= i_end:
                continue
            if i_start < image_cursor_end:
                print(f"  [skip] 画像の時間帯が前の項目と重複: index={cue['index']} "
                      f"({cue['start']}s)", file=sys.stderr)
                n_skip_image += 1
                continue
            text = add_image_clip(text, allocator, path, i_start, i_end, image_track_uid)
            image_cursor_end = i_end
            n_image += 1

    print(f"align.json適用結果: 基本テロップ{n_base}件 / 強調テロップ{n_telop}件 / SE{n_se}件 / "
          f"画像{n_image}件 / "
          f"スキップ(スタイル不明{n_skip_style}件, テロップ重複{n_skip_telop}件, SE重複{n_skip_se}件, "
          f"画像{n_skip_image}件)",
          file=sys.stderr)
    return text


def load_style_uid_map(style_json_path: str) -> dict:
    import json
    with open(style_json_path, encoding="utf-8") as f:
        d = json.load(f)
    return d.get("style_name_to_uid", {})


def generate_prproj(template_path: str, audio_path: str, output_path: str,
                     align_json_path: str, style_json_path: str,
                     style_se_categories_path: str = None, image_dir: str = None,
                     sequence_name: str = None) -> None:
    """テンプレ.prprojを複製し、ナレーション・トラック・テロップ・SE・画像を
    すべて差し込んだ.prprojをoutput_pathへ書き出す(auto-telop CLIの
    中核処理)。特定案件専用のUID類はテンプレから都度自動検出する。"""
    text = load_prproj_text(template_path)
    allocator = ObjectIdAllocator(text)

    sequence_uid = find_sequence_uid(text, sequence_name)
    print(f"シーケンスを検出しました: {sequence_uid}", file=sys.stderr)

    text, info = import_narration_audio(text, allocator, audio_path)
    print(f"ナレーションをインポートしました: MasterClip={info['master_clip_uid']} "
          f"duration_ticks={info['duration_ticks']}", file=sys.stderr)

    audio_group_id = _find_track_group_object_id(text, sequence_uid, "AudioTrackGroup")
    try:
        narration_track_uid, se_track_uid, se_track_uid2 = find_empty_track_uids(
            text, audio_group_id, "AudioTrackGroup", "AudioClipTrack", 3)
        print(f"空きオーディオトラックを検出しました: ナレーション用={narration_track_uid} "
              f"SE用={se_track_uid} SE用(2本目)={se_track_uid2}", file=sys.stderr)
    except ValueError:
        # SE用に2本目の空きトラックが無いテンプレでは、従来通り1本のみで
        # 動作させる(SEの時間帯が詰まっている箇所は取りこぼしが増えるが
        # 致命的な失敗にはしない)。
        narration_track_uid, se_track_uid = find_empty_track_uids(
            text, audio_group_id, "AudioTrackGroup", "AudioClipTrack", 2)
        se_track_uid2 = None
        print(f"空きオーディオトラックを検出しました: ナレーション用={narration_track_uid} SE用={se_track_uid} "
              f"(SE用の2本目は空きトラックが無いため使用しません)", file=sys.stderr)

    text = place_audio_on_track(
        text, allocator, narration_track_uid,
        master_clip_uid=info["master_clip_uid"],
        markers_id=info["markers_id"],
        source_id=info["source_id"],
        display_name=info["display_name"],
        start_ticks=0,
        duration_ticks=info["duration_ticks"],
        donor_master_clip_uid=info["donor_master_clip_uid"],
        donor_name=info["donor_name"],
    )
    print("ナレーションをシーケンスに配置しました。", file=sys.stderr)

    # 基本テロップ(V8)・強調テロップ(V9)を指定の位置に置くため、既存V1-V4の
    # 続きにV5-V9を追加する(V5-V6は未使用の予備トラック、V7は画像用に使う)。
    # 画像は基本テロップ(V8)より下のトラックに置き、テキストが画像の上に
    # 重なって表示されるようにする。
    text, new_video_tracks = add_video_tracks(text, allocator, 5, sequence_uid)
    image_track_uid, base_track_uid, telop_track_uid = new_video_tracks[-3], new_video_tracks[-2], new_video_tracks[-1]
    print(f"新規ビデオトラックを追加しました(V5-V9): 画像用(V7)={image_track_uid} "
          f"基本テロップ用(V8)={base_track_uid} 強調テロップ用(V9)={telop_track_uid}", file=sys.stderr)

    image_map = {}
    if image_dir:
        for fname in os.listdir(image_dir):
            m = re.match(r"(\d+)_", fname)
            if m:
                image_map[int(m.group(1))] = os.path.join(image_dir, fname)
        print(f"画像ディレクトリから{len(image_map)}件のindex->ファイル対応を読み込みました。", file=sys.stderr)

    text = apply_align_json(text, allocator, align_json_path, style_json_path, style_se_categories_path,
                             telop_track_uid=telop_track_uid, base_track_uid=base_track_uid,
                             se_track_uid=se_track_uid, se_track_uid2=se_track_uid2,
                             image_track_uid=image_track_uid, image_map=image_map)

    save_prproj_text(output_path, text)
    print(f"出力しました: {output_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="align.json出力からテロップ入りprprojを生成する")
    parser.add_argument("--template", required=True, help="テンプレ.prprojのパス(スタイルカタログ)")
    parser.add_argument("--audio", required=True, help="新規ナレーション音声ファイル")
    parser.add_argument("-o", "--output", required=True, help="出力prprojパス")
    parser.add_argument("--style-json", required=True,
                         help="analyze-templateで生成したスタイルカタログJSONのパス")
    parser.add_argument("--style-se-categories",
                         help="スタイル名->SEファイルの固定対応表JSON(省略可。省略時はカテゴリ別スケール調整・"
                              "固定SEペアリングを行わない)")
    parser.add_argument("--sequence-name", help="テンプレ内で対象とするSequence名(省略時は最初のもの)")
    parser.add_argument("--align", required=True,
                         help="align.py が出力したJSONのパス。cues全件のテロップ・SE・画像をまとめて差し込む")
    parser.add_argument("--image-dir",
                         help="align.jsonのimage_cuesに対応する画像ファイルが入ったディレクトリ。"
                              "各ファイル名は '<index>_...' (indexはimage_cuesのindexと一致)で始まる"
                              "必要がある(例: 03_毎日残業して.jpg)。")
    args = parser.parse_args()

    generate_prproj(
        template_path=args.template, audio_path=args.audio, output_path=args.output,
        align_json_path=args.align, style_json_path=args.style_json,
        style_se_categories_path=args.style_se_categories, image_dir=args.image_dir,
        sequence_name=args.sequence_name,
    )


if __name__ == "__main__":
    main()
