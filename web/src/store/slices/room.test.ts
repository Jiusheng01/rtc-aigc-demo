import { describe, expect, jest, test } from '@jest/globals';
import reducer, {
  localLeaveRoom,
  updateAIGCState,
  updateAIReadyState,
  updateScene,
  updateSceneConfig,
  setHistoryMsg,
} from './room';

jest.mock('@volcengine/rtc', () => ({
  NetworkQuality: { UNKNOWN: 0 },
}));

jest.mock('@/lib/RtcClient', () => ({
  __esModule: true,
  default: { setBasicInfo: () => undefined },
}));

describe('room AI readiness', () => {
  test('tracks conversation readiness independently from subtitles', () => {
    let state = reducer(undefined, { type: 'room/init' });

    state = reducer(state, updateAIGCState({ isAIGCEnable: true }));
    state = reducer(state, updateAIReadyState({ isAIReady: true }));

    expect(state.msgHistory).toHaveLength(0);
    expect(state.isAIReady).toBe(true);
  });

  test('resets conversation readiness when leaving', () => {
    let state = reducer(undefined, { type: 'room/init' });
    state = reducer(state, updateAIGCState({ isAIGCEnable: true }));
    state = reducer(state, updateAIReadyState({ isAIReady: true }));

    state = reducer(state, localLeaveRoom());

    expect(state.isAIGCEnable).toBe(false);
    expect(state.isAIReady).toBe(false);
  });
});

test('preserves incremental avatar subtitles and paragraph boundaries', () => {
  let state = reducer(undefined, { type: 'room/init' });
  state = reducer(state, updateScene('avatar'));
  state = reducer(
    state,
    updateSceneConfig({
      avatar: { id: 'avatar', botName: 'agent', isAvatarScene: true },
    })
  );
  for (const [text, paragraph] of [
    ['hello ', false],
    ['world', true],
    ['next', false],
  ]) {
    state = reducer(state, setHistoryMsg({ user: 'agent', text, paragraph, definite: true }));
  }
  expect(state.msgHistory.map(({ value }) => value)).toEqual(['hello world', 'next']);
});
