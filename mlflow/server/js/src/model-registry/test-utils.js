export const mockRegisteredModelDetailed = (name, latestVersions = [], tags = []) => {
  return {
    creationTimestamp: 1571344731467,
    lastUpdatedTimestamp: 1573581360069,
    latest_versions: latestVersions,
    name,
    tags,
  };
};

export const mockModelVersionDetailed = (
  name,
  version,
  stage,
  status,
  tags = [],
  runLink = undefined,
  runId = 'b99a0fc567ae4d32994392c800c0b6ce',
) => {
  return {
    name,
    // Use version-based timestamp to make creationTimestamp differ across model versions
    // and prevent React duplicate key warning.
    creationTimestamp: version.toString(),
    lastUpdatedTimestamp: (version + 1).toString(),
    user_id: 'richard@example.com',
    currentStage: stage,
    description: '',
    source: 'path/to/model',
    runId: runId,
    runLink: runLink,
    status,
    version,
    tags,
  };
};

export const mockGetFieldValue = (comment, archive) => {
  return (key) => {
    if (key === 'comment') {
      return comment;
    } else if (key === 'archiveExistingVersions') {
      return archive;
    }
    throw new Error('Missing mockGetFieldValue key');
  };
};
